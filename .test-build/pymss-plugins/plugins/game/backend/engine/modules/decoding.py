import math

import torch
import torch.nn.functional as F
from torch import Tensor

from ..deployment.context import is_export_mode


def find_local_extremum(x: Tensor, threshold: float = None, radius: int = 2, maxima=True):
    """
    Find local extremum in the last dimension of x within a given radius and below/above a threshold.
    :param x: [..., T]
    :param threshold: ignore maxima below this threshold or minima above this threshold.
    :param radius: int, radius for local extremum search. x[i] is considered a local extremum
     only if it is the maximum/minimum within the window [i-radius, i+radius].
    :param maxima: bool, if True, find local maxima; otherwise, find local minima
    :return: [..., T], 1 = extremum, 0 = non-extremum
    """
    if not is_export_mode():
        assert radius >= 1
    infinite = float("+inf") if maxima else float("-inf")
    x_pad = F.pad(
        x, (radius, radius),
        mode="constant", value=infinite
    )  # [..., T + 2r]
    if is_export_mode():
        *B, T = x.shape
        nB = x.ndim - 1
        frame_idx = torch.arange(T, device=x.device, dtype=torch.long)  # [T]
        window_idx = torch.arange(2 * radius + 1, device=x.device, dtype=torch.long)  # [2r+1]
        unfold_idx = (frame_idx.unsqueeze(-1) + window_idx.unsqueeze(0))  # [T, 2r+1]
        x_src = x_pad.unsqueeze(-2).expand(*((-1,) * nB), T, -1)  # [..., T, T+2r]
        index = unfold_idx.reshape(*((1,) * nB), T, -1).expand(*B, -1, -1)  # [..., T, 2r+1]
        windows = x_src.gather(dim=-1, index=index)  # [..., T, 2r+1]
    else:
        windows = x_pad.unfold(dimension=-1, size=2 * radius + 1, step=1)  # [..., T, 2r+1]
    if maxima:
        maxima = (windows.argmax(dim=-1) == radius)  # [..., T]
        if threshold is not None:
            maxima &= (x >= threshold)
        return maxima
    else:
        minima = (windows.argmin(dim=-1) == radius)  # [..., T]
        if threshold is not None:
            minima &= (x <= threshold)
        return minima


def decode_soft_boundaries(
        boundaries: Tensor, barriers: Tensor = None, mask: Tensor = None,
        threshold: float = 0.5, radius: int = 2
):
    """
    Decode gaussian-softened boundaries.
    :param boundaries: boundary probabilities within [0, 1]
    :param barriers: [..., T], optional preset boundaries
    :param mask: [..., T], optional mask
    :param threshold: float(0~1), only consider probabilities above this value
    :param radius: int, radius for local maxima search
    :return:
    """
    if mask is not None:
        boundaries = torch.where(mask, boundaries, float("+inf"))
    if barriers is not None:
        boundaries = torch.masked_fill(boundaries, barriers, float("+inf"))
    maxima = find_local_extremum(boundaries, threshold=threshold, radius=radius, maxima=True)
    if mask is not None:
        maxima &= mask
    return maxima


def decode_boundaries_from_velocities(
        velocities: Tensor, barriers: Tensor = None, mask: Tensor = None,
        threshold: float = 0.2, radius: int = 2
):
    """
    Decode boundary indicators from predicted velocities. Steps:
        1. Accumulate velocities to get distances.
        2. Apply min-max normalization to distances.
        3. Find local minima in distances that are below the threshold.
    :param velocities: [..., T]
    :param barriers: [..., T], optional preset boundaries or local minima points
    :param mask: [..., T], optional mask to apply before decoding
    :param threshold: float (0~1), velocity threshold (after normalization) to consider a boundary
    :param radius: int, radius for local minima search
    :return: [..., T], 1 = boundary, 0 = non-boundary
    """
    distances = velocities.cumsum(dim=-1)
    if mask is not None:
        distances_upper_masked = torch.where(mask, distances, float("+inf"))
        distances_lower_masked = torch.where(mask, distances, float("-inf"))
    else:
        distances_upper_masked = distances
        distances_lower_masked = distances
    d_min = distances_upper_masked.amin(dim=-1, keepdim=True)
    d_max = distances_lower_masked.amax(dim=-1, keepdim=True)
    distances = (distances - d_min) / (d_max - d_min + 1e-8)
    if mask is not None:
        distances = torch.where(mask, distances, float("-inf"))
    if barriers is not None:
        distances = torch.masked_fill(distances, barriers, float("-inf"))
    boundaries = find_local_extremum(distances, threshold=threshold, radius=radius, maxima=False)  # [..., T]
    if mask is not None:
        boundaries &= mask
    return boundaries


def decode_cascaded_dial_pointers(
        beam: Tensor, dials: Tensor, periods: list[float]
):
    """
    Decode cascaded dial pointers one-by-one to refine the beam values.
    For each iteration, beam := [(beam - dial) / period] * period + dial
    :param beam: [...], initial beam values
    :param dials: [..., N, 2], cascaded dial pointers in Cartesian coordinates
    :param periods: list of float, periods for each dial
    :return: [...], refined beam values
    """
    angles = torch.atan2(dials[..., 1], dials[..., 0])  # [..., N]
    for period, angle in zip(periods, angles.unbind(dim=-1)):
        dial = (angle / (2 * torch.pi)) * period  # [...]
        k = torch.round((beam - dial) / period)  # [...]
        beam = k * period + dial  # [...]
    return beam


def decode_gaussian_blurred_probs(
        probs: Tensor,
        min_val: float, max_val: float, deviation: float,
        threshold: float = 0.1
):
    """
    Decode gaussian-blurred probabilities to continuous values and presence flags.
    :param probs: [..., N]
    :param min_val: value of the lowest bin
    :param max_val: value of the highest bin
    :param deviation: deviation of the gaussian blur in the original value scale
    :param threshold: presence threshold
    :return: values [...], presence [...]
    """
    B = (1,) * (probs.ndim - 1)
    N = probs.shape[-1]
    width = math.ceil(deviation / (max_val - min_val) * (N - 1))
    idx = torch.arange(N, dtype=torch.long, device=probs.device).reshape(*B, -1)  # [..., N]
    center_values = torch.linspace(min_val, max_val, steps=N, device=probs.device).reshape(*B, -1)  # [..., N]
    centers = torch.argmax(probs, dim=-1, keepdim=True)  # [..., 1]
    start = torch.clip(centers - width, min=0)  # [..., 1]
    end = torch.clip(centers + width + 1, max=N)  # [..., 1]
    idx_masks = (idx >= start) & (idx < end)  # [..., N]
    weights = probs * idx_masks  # [..., N]
    product_sum = torch.sum(weights * center_values, dim=-1)  # [...]
    weight_sum = torch.sum(weights, dim=-1)  # [..., T]
    values = product_sum / (weight_sum + 1e-8)  # avoid dividing by zero, [..., T]
    presence = probs.amax(dim=-1) >= threshold  # [..., T]
    return values, presence
