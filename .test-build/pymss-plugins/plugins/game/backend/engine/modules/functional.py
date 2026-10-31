import torch
from torch import Tensor
from torch.nn import functional as F


def boundaries_to_regions(boundaries: Tensor, mask: Tensor = None):
    regions = torch.cumsum(boundaries.long(), dim=-1) + 1  # [..., T]
    if mask is not None:
        regions = regions * mask.long()
    return regions


def regions_to_boundaries(regions: Tensor):
    boundaries = F.pad(
        regions[..., 1:] > regions[..., :-1], (1, 0),
        mode="constant", value=False
    )
    return boundaries


def regions_to_durations(regions: Tensor, max_n: int = None):
    B = regions.shape[:-1]
    if max_n is None:
        max_n = regions.max()
    durations = regions.new_zeros((*B, max_n + 1)).scatter_add(
        dim=-1, index=regions, src=torch.ones_like(regions)
    )[..., 1:]
    return durations


def flatten_sequences(x: Tensor, idx: Tensor):
    return torch.gather(
        F.pad(x, (1, 0), value=0), dim=-1, index=idx
    )


def format_boundaries(
        durations: Tensor,
        length: int | Tensor,
        timestep: float,
) -> tuple[Tensor, Tensor]:
    boundary_indices = durations.cumsum(dim=1).div(timestep).round().long().unsqueeze(1)  # [B, 1, N]
    indices = torch.arange(length, dtype=torch.long, device=durations.device)[None, ..., None]  # [1, T, 1]
    boundaries = (indices == boundary_indices[..., :-1]).any(dim=2)  # [B, T]
    return boundaries
