from typing import Any

import torch
from torch import nn

from ..lib.reflection import build_object_from_class_name
from ..modules.commons.common_layers import CyclicRegionEmbedding


class SegmentationEstimationModel(nn.Module):
    def __init__(self, config: Any):
        super().__init__()
        self.embedding_dim = config.embedding_dim
        # Encoder
        self.spectrogram_projection = nn.Linear(config.in_dim, self.embedding_dim)
        self.encoder = build_object_from_class_name(
            config.encoder.cls, nn.Module,
            self.embedding_dim, 2 * self.embedding_dim, False,
            **config.encoder.kwargs
        )
        # Segmenter
        self.noise_embedding = CyclicRegionEmbedding(
            self.embedding_dim,
            cycle_length=config.region_cycle_len
        )
        self.mode = config.mode
        if self.mode == "d3pm":
            self.time_embedding = nn.Sequential(
                nn.Linear(1, self.embedding_dim * 4),
                nn.GELU(),
                nn.Linear(self.embedding_dim * 4, self.embedding_dim)
            )
        self.use_language_embedding = config.use_languages
        if self.use_language_embedding:
            self.language_embedding = nn.Embedding(config.num_languages + 1, self.embedding_dim, padding_idx=0)
        self.segmenter = build_object_from_class_name(
            config.segmenter.cls, nn.Module,
            self.embedding_dim, 1, True,
            **config.segmenter.kwargs
        )
        # Estimator
        self.region_embedding = CyclicRegionEmbedding(
            self.embedding_dim,
            cycle_length=config.region_cycle_len
        )
        self.estimator = build_object_from_class_name(
            config.estimator.cls, nn.Module,
            self.embedding_dim, config.estimator_out_dim,
            **config.estimator.kwargs
        )

    def forward_encoder(self, spectrogram, mask=None):
        x = self.spectrogram_projection(spectrogram)
        x = self.encoder(x, mask=mask)
        x_seg, x_est = torch.split(x, [self.embedding_dim, self.embedding_dim], dim=-1)
        return x_seg, x_est

    def forward_segmentation(self, x, noise, t=None, language=None, mask=None):
        x = x + self.noise_embedding(noise)
        if self.mode == "d3pm":
            x = x + self.time_embedding(t[..., None, None])
        if self.use_language_embedding:
            x = x + self.language_embedding(language.unsqueeze(-1))
        x, latent = self.segmenter(x, mask=mask)
        x = x.squeeze(-1)
        return x, latent

    def forward_estimation(self, x, regions, t_mask=None, n_mask=None):
        x = x + self.region_embedding(regions)
        _, x_down = self.estimator(x, regions, t_mask=t_mask, n_mask=n_mask)
        return x_down
