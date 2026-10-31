import torch
from torch import nn


class CyclicRegionEmbedding(nn.Module):
    def __init__(self, embedding_dim: int, cycle_length: int = 3):
        super().__init__()
        self.cycle_length = cycle_length
        self.embedding = nn.Embedding(cycle_length, embedding_dim)

    def forward(self, idx):
        if self.training:
            *B, _ = idx.shape
            shift = torch.randint(0, self.cycle_length, (*B, 1)).to(idx)
            idx = idx + shift
        return self.embedding(idx % self.cycle_length)


class LocalDownsample(nn.Module):
    # noinspection PyMethodMayBeStatic
    def forward(self, x, regions, max_n: int = None):
        """
        :param x: [..., T, C] input tensor to downsample
        :param regions: int64 [..., T] mapping from positions to region indices starting from 1.
        :param max_n: int, maximum number of regions. N = max(regions) if not given.
        :return: [..., N, C] where N = max(regions)
        """
        N = regions.max() if max_n is None else max_n
        B = (1,) * (x.ndim - 2)
        idx = torch.arange(N + 1, dtype=torch.long, device=regions.device).reshape(*B, -1, 1)  # [..., N+1, 1]
        region_map = idx == regions.unsqueeze(-2)  # [..., N, T]
        region_weight = region_map.float()
        region_size = torch.where(
            torch.any(region_map, dim=-1, keepdim=True),
            region_weight.sum(dim=-1, keepdim=True),
            1.0
        )  # [..., N, 1]
        weight = region_weight / region_size  # [..., N+1, T]
        weight = weight[..., 1:, :]  # [..., N, T]
        x_down = weight @ x  # [..., N, T] @ [..., T, C] -> [..., N, C]
        return x_down  # [..., N, C]
