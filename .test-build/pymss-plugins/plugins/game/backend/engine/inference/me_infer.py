from __future__ import annotations

from typing import Any

import torch
from torch import nn, Tensor

from ..lib.feature.mel import StretchableMelSpectrogram
from ..modules.d3pm import (
    d3pm_time_schedule,
    remove_mutable_boundaries,
)
from ..modules.decoding import (
    decode_soft_boundaries,
    decode_gaussian_blurred_probs,
)
from ..modules.functional import (
    format_boundaries,
    boundaries_to_regions,
    regions_to_durations,
)
from ..modules.midi_extraction import SegmentationEstimationModel


class SegmentationEstimationInferenceModel(nn.Module):
    def __init__(
            self,
            model_config: Any,
            inference_config: Any,
    ):
        super().__init__()
        self.model_config = model_config
        self.inference_config = inference_config
        self.timestep = self.inference_config.features.timestep
        self.to_spectrogram = StretchableMelSpectrogram(
            sample_rate=inference_config.features.audio_sample_rate,
            n_mels=inference_config.features.spectrogram.num_bins,
            n_fft=inference_config.features.fft_size,
            win_length=inference_config.features.win_size,
            hop_length=inference_config.features.hop_size,
            fmin=inference_config.features.spectrogram.fmin,
            fmax=inference_config.features.spectrogram.fmax,
            clip_val=1e-5,
        )
        self.model = SegmentationEstimationModel(model_config)

    def forward_and_decode_boundaries(
            self, x_seg, known_boundaries, prev_boundaries,
            mask, threshold, radius,
            language=None, t=None,
    ):
        B = x_seg.size(0)
        if self.model_config.mode == "d3pm":
            t = t.unsqueeze(0).expand(B)
            p = d3pm_time_schedule(t)
            boundaries = remove_mutable_boundaries(prev_boundaries, known_boundaries, p=p)
        elif self.model_config.mode == "completion":
            boundaries = known_boundaries
        else:
            raise ValueError(f"Unknown mode: {self.model_config.mode}.")
        noise = boundaries_to_regions(boundaries, mask=mask)  # [B, T]
        logits, _ = self.model.forward_segmentation(
            x_seg, noise=noise, t=t,
            language=language, mask=mask,
        )  # [B, T]
        soft_boundaries = logits.sigmoid()
        boundaries = decode_soft_boundaries(
            boundaries=soft_boundaries,
            barriers=known_boundaries, mask=mask,
            threshold=threshold, radius=radius,
        )  # [B, T]
        return boundaries

    def forward_and_decode_scores(
            self, x_est: Tensor, regions: Tensor,
            t_mask: Tensor, n_mask: Tensor,
            threshold: Tensor,
    ) -> tuple[Tensor, Tensor]:
        logits = self.model.forward_estimation(
            x_est, regions=regions, t_mask=t_mask, n_mask=n_mask,
        )  # [B, N, C_out]
        probs = logits.sigmoid()
        scores, presence = decode_gaussian_blurred_probs(
            probs=probs,
            min_val=self.inference_config.midi_min,
            max_val=self.inference_config.midi_max,
            deviation=self.inference_config.midi_std * 3,  # use 3 std as the decoding deviation
            threshold=threshold,
        )
        presence = presence & n_mask
        scores = scores * presence.float()
        return presence, scores

    def forward_encoder_main(
            self, spectrogram: Tensor, mask: Tensor,
    ) -> tuple[Tensor, Tensor]:
        x_seg, x_est = self.model.forward_encoder(spectrogram, mask=mask)
        return x_seg, x_est

    def forward_encoder(
            self, waveform: Tensor, duration: Tensor,
    ) -> tuple[Tensor, Tensor, Tensor]:
        spectrogram = self.to_spectrogram(waveform).transpose(-2, -1)  # [B, T, C]
        T = spectrogram.size(1)
        L = duration.div(self.timestep).round().long()  # [B]
        idx = torch.arange(T, dtype=torch.long, device=duration.device)  # [T]
        mask = idx.unsqueeze(0) < L.unsqueeze(1)  # [B, T]
        x_seg, x_est = self.forward_encoder_main(spectrogram, mask=mask)
        return x_seg, x_est, mask

    def forward_segmenter_main(
            self, x_seg, known_boundaries, mask,
            threshold, radius,
            language=None, t=None,
    ):
        if self.model_config.mode == "d3pm":
            boundaries = known_boundaries
            for ti in t:
                boundaries = self.forward_and_decode_boundaries(
                    x_seg, known_boundaries=known_boundaries,
                    prev_boundaries=boundaries, t=ti,
                    language=language, mask=mask,
                    threshold=threshold, radius=radius,
                )  # [B, T]
        elif self.model_config.mode == "completion":
            boundaries = self.forward_and_decode_boundaries(
                x_seg, known_boundaries=known_boundaries,
                prev_boundaries=None, t=None,
                mask=mask, language=language,
                threshold=threshold, radius=radius,
            )  # [B, T]
        else:
            raise ValueError(f"Unknown mode: {self.model_config.mode}.")

        regions = boundaries_to_regions(boundaries, mask=mask)  # [B, T]
        max_n = regions.max()

        return boundaries, regions, max_n

    def forward_segmenter(
            self, x_seg: Tensor, known_durations: Tensor, mask: Tensor,
            threshold: Tensor, radius: Tensor,
            language: Tensor = None, t: Tensor = None,
    ) -> tuple[Tensor, Tensor, Tensor]:
        T = x_seg.size(1)
        known_boundaries = format_boundaries(
            durations=known_durations, length=T, timestep=self.timestep
        )  # [B, T]
        known_boundaries = known_boundaries & mask

        boundaries, regions, max_n = self.forward_segmenter_main(
            x_seg, known_boundaries=known_boundaries, mask=mask,
            language=language, t=t,
            threshold=threshold, radius=radius,
        )  # [B, T]

        durations = regions_to_durations(regions, max_n=max_n) * self.timestep  # [B, N]
        return durations, regions, max_n

    def forward_estimator(
            self, x_est: Tensor, regions: Tensor, mask: Tensor,
            max_n: int, threshold: Tensor,
    ) -> tuple[Tensor, Tensor]:
        idx = torch.arange(max_n, dtype=torch.long, device=regions.device).unsqueeze(0)  # [1, N]
        max_idx = regions.amax(dim=-1, keepdim=True)  # [B, 1]
        n_mask = idx < max_idx  # [B, N]
        presence, scores = self.forward_and_decode_scores(
            x_est, regions=regions, t_mask=mask, n_mask=n_mask,
            threshold=threshold,
        )
        return presence, scores

    def forward(
            self, waveform: Tensor,
            known_durations: Tensor,
            boundary_threshold: Tensor,
            boundary_radius: Tensor,
            score_threshold: Tensor,
            language: Tensor = None,
            t: Tensor = None,
    ) -> tuple[Tensor, Tensor, Tensor]:
        """
        :param waveform: float32 [batch_size, num_samples]
        :param known_durations: float32 [batch_size, num_known_regions]
        :param boundary_threshold: float32 scalar
        :param boundary_radius: int64 scalar
        :param score_threshold: float32 scalar
        :param language: int64 [batch_size]
        :param t: float32 [num_steps]
        :return: durations: float32; presence: bool; scores: float32 [batch_size, num_notes]
        """
        waveform_duration = known_durations.sum(dim=1)  # [B]
        # Encoder
        x_seg, x_est, mask = self.forward_encoder(
            waveform=waveform, duration=waveform_duration
        )
        # Segmentation
        durations, regions, max_n = self.forward_segmenter(
            x_seg, known_durations=known_durations, mask=mask,
            language=language, t=t,
            threshold=boundary_threshold, radius=boundary_radius,
        )
        # Estimation
        presence, scores = self.forward_estimator(
            x_est, regions=regions, mask=mask,
            max_n=max_n, threshold=score_threshold,
        )

        return durations, presence, scores
