import numpy as np
import torch
import torch.nn.functional as F
import torch.utils.data
from librosa.filters import mel as librosa_mel_fn

from ...deployment.context import is_export_mode


def dynamic_range_compression_torch(x, C=1, clip_val=1e-5):
    return torch.log(torch.clamp(x, min=clip_val) * C)


class StretchableMelSpectrogram(torch.nn.Module):
    def __init__(
            self,
            sample_rate=44100,
            n_mels=128,
            n_fft=2048,
            win_length=2048,
            hop_length=512,
            fmin=40,
            fmax=16000,
            clip_val=1e-5
    ):
        super().__init__()
        self.sample_rate = sample_rate
        self.n_mels = n_mels
        self.n_fft = n_fft
        self.win_size = win_length
        self.hop_length = hop_length
        self.fmin = fmin
        self.fmax = fmax
        self.clip_val = clip_val

        mel_basis = librosa_mel_fn(sr=sample_rate, n_fft=n_fft, n_mels=n_mels, fmin=fmin, fmax=fmax)
        self.register_buffer("mel_basis", torch.from_numpy(mel_basis).float(), persistent=False)

    def forward(self, y, key_shift=0, speed=1, center=False):
        export_mode = is_export_mode()

        factor = 2 ** (key_shift / 12)
        n_fft_new = int(np.round(self.n_fft * factor))
        win_length_new = int(np.round(self.win_size * factor))
        hop_length_new = int(np.round(self.hop_length * speed))

        window = torch.hann_window(win_length_new, device=self.mel_basis.device)

        y = torch.nn.functional.pad(y, (
            (win_length_new - hop_length_new) // 2,
            (win_length_new - hop_length_new + 1) // 2
        ), mode='reflect')

        return_complex = not export_mode
        spec = torch.stft(
            y, n_fft_new, hop_length=hop_length_new,
            win_length=win_length_new, window=window,
            center=center, pad_mode='reflect',
            normalized=False, onesided=True, return_complex=return_complex
        )
        if return_complex:
            spec = spec.abs()
        else:
            spec = torch.sqrt(torch.sum(spec ** 2, dim=-1))
        if key_shift != 0:
            size = self.n_fft // 2 + 1
            resize = spec.size(1)
            if resize < size:
                spec = F.pad(spec, (0, 0, 0, size - resize))
            spec = spec[:, :size, :] * self.win_size / win_length_new

        spec = torch.matmul(self.mel_basis, spec)
        spec = dynamic_range_compression_torch(spec, clip_val=self.clip_val)

        return spec
