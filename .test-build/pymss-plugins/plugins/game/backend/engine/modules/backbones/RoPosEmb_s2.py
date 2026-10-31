import torch
import torch.nn as nn


def compute_inv_freq(dim: int, theta: float = 10000.0):
    """pre-compute inv_freq, dim is fixed"""
    return 1.0 / (theta ** (torch.arange(0, dim, 2)[: (dim // 2)].float() / dim))


def compute_freqs_cis_dynamic(x: torch.Tensor, inv_freq: torch.Tensor):
    """ONNX兼容：动态计算cos/sin，序列长度从xa tensor shape获取"""
    # 用tensor操作获取seq_len，让ONNX能动态trace
    seq_len = x.shape[-2]
    # 生成位置索引 [0, 1, 2, ..., seq_len-1]
    t = torch.arange(seq_len, device=x.device, dtype=inv_freq.dtype)
    # outer product: [seq_len, dim//2]
    freqs = torch.outer(t, inv_freq)
    return torch.cos(freqs), torch.sin(freqs)


def single_apply_rotary_emb(
        x: torch.Tensor,
        freqs_cos: torch.Tensor,
        freqs_sin: torch.Tensor,
):
    """ONNX兼容：手动实现复数乘法"""
    x_ = x.float().reshape(*x.shape[:-1], -1, 2).contiguous()

    # 分离实部和虚部
    x_r, x_i = x_[..., 0], x_[..., 1]

    # 复数乘法: (x_r + x_i*j) * (cos + sin*j) = (x_r*cos - x_i*sin) + (x_r*sin + x_i*cos)*j
    x_out_r = x_r * freqs_cos - x_i * freqs_sin
    x_out_i = x_r * freqs_sin + x_i * freqs_cos

    # 合并实部和虚部
    x_out = torch.stack([x_out_r, x_out_i], dim=-1).flatten(-2)

    return x_out.type_as(x)


class SingleRoPosEmb(nn.Module):
    def __init__(self, dim: int, max_len=5000, theta=10000.0, use_cache=True):
        super().__init__()
        self.dim = dim
        self.theta = theta
        self.use_cache = use_cache
        # inv_freq是固定的，可以预计算
        self.register_buffer('inv_freq', compute_inv_freq(dim, theta), persistent=False)
        # 缓存模式下预计算pe
        if use_cache:
            pe_cos, pe_sin = compute_freqs_cis_dynamic(
                torch.zeros(1, max_len, dim), self.inv_freq)
            self.register_buffer('pe_cos', pe_cos[None, :, :], persistent=False)
            self.register_buffer('pe_sin', pe_sin[None, :, :], persistent=False)

    def extend_pe(self, x):
        """Reset the positional encodings (only for use_cache=True mode)."""
        if self.pe_cos.size(1) >= x.size(-2):
            return
        pe_cos, pe_sin = compute_freqs_cis_dynamic(x, self.inv_freq)
        self.pe_cos = pe_cos[None, :, :].to(device=x.device)
        self.pe_sin = pe_sin[None, :, :].to(device=x.device)

    def get_pe_dynamic(self, x):
        """ONNX模式：完全动态计算"""
        ndim = x.ndim
        seq_len = x.shape[-2]
        pe_cos, pe_sin = compute_freqs_cis_dynamic(x, self.inv_freq)
        pe_cos = pe_cos.view(*((1,) * (ndim - 2)), seq_len, self.dim // 2)
        pe_sin = pe_sin.view(*((1,) * (ndim - 2)), seq_len, self.dim // 2)
        return pe_cos, pe_sin

    def get_pe_cached(self, x):
        """Cache模式：从缓存切片"""
        ndim = x.ndim
        seq_len = x.size(-2)
        pe_cos = self.pe_cos[:, :seq_len]
        pe_sin = self.pe_sin[:, :seq_len]
        pe_cos = pe_cos.view(*((1,) * (ndim - 2)), seq_len, self.dim // 2)
        pe_sin = pe_sin.view(*((1,) * (ndim - 2)), seq_len, self.dim // 2)
        return pe_cos, pe_sin

    def forward(self, x):
        if self.use_cache:
            self.extend_pe(x)
            pe_cos, pe_sin = self.get_pe_cached(x)
        else:
            # ONNX模式：完全动态计算
            pe_cos, pe_sin = self.get_pe_dynamic(x)
        return single_apply_rotary_emb(x, pe_cos, pe_sin)
