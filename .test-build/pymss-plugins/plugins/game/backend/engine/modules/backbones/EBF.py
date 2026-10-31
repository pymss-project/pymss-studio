from typing import Optional

import torch
from torch import nn
import torch.nn.functional as F

from ...deployment.context import is_export_mode
from ...modules.backbones.RoPosEmb_s2 import SingleRoPosEmb
from ...modules.backbones.eglu import HalfCacheGLUFFN


def _to_heads(value, num_heads):
    batch, length, _ = value.shape
    return value.reshape(batch, length, num_heads, -1).permute(0, 2, 1, 3)


def _from_heads(value):
    batch, heads, length, width = value.shape
    return value.permute(0, 2, 1, 3).reshape(batch, length, heads * width)


class LayScale(nn.Module):
    def __init__(self, dim, lay_scale_init_value=1e-6):
        super().__init__()
        sp = torch.ones(dim) * lay_scale_init_value

        self.scale = nn.Parameter(sp)

        self.dim = dim

    def unc(self, res):
        n_dim = res.ndim
        if n_dim == 1:
            return self.scale
        else:
            return self.scale.view(*((1,) * (n_dim - 1)), self.dim)

    def forward(self, x):
        return x * self.unc(x)


class RMSnorm(torch.nn.Module):
    def __init__(self, dim: int, init_num=1, eps: float = 1e-6):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim) * init_num)

    def _norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)

    def forward(self, x):
        output = self._norm(x)
        return output * self.weight


class GLUFFN(nn.Module):
    def __init__(self, dim, latent_dim=None, dropout_latent: float = 0.1, dropout_output: float = 0.1):
        super().__init__()
        if latent_dim is None:
            latent_dim = dim * 4
        self.ln1 = nn.Linear(dim, latent_dim * 2)

        self.ln2 = nn.Linear(latent_dim, dim)
        self.dropout_latent = nn.Dropout(dropout_latent) if dropout_latent > 0. else nn.Identity()
        self.dropout_output = nn.Dropout(dropout_output) if dropout_output > 0. else nn.Identity()

    def forward(self, x):
        x1, x2 = self.ln1(x).chunk(2, dim=-1)
        x = F.gelu(x1) * x2
        x = self.dropout_latent(x)
        x = self.ln2(x)
        return self.dropout_output(x)


class FFN(nn.Module):
    def __init__(self, dim, latent_dim=None, dropout_latent: float = 0.1, dropout_output: float = 0.1):
        super().__init__()
        if latent_dim is None:
            latent_dim = dim * 4
        self.ln1 = nn.Linear(dim, latent_dim)
        self.ln2 = nn.Linear(latent_dim, dim)
        self.dropout_latent = nn.Dropout(dropout_latent) if dropout_latent > 0. else nn.Identity()
        self.dropout_output = nn.Dropout(dropout_output) if dropout_output > 0. else nn.Identity()

    def forward(self, x):
        x = self.ln1(x)
        x = F.gelu(x)
        x = self.dropout_latent(x)
        x = self.ln2(x)
        return self.dropout_output(x)


class CgMLP(nn.Module):
    def __init__(
            self, dim: int,
            kernel_size: int = 31,
            out_drop=0.1,
            latent_drop=0.0,
            bias: bool = True,
            use_dw_act=True,
            latent_dim: Optional[int] = None
    ):
        super().__init__()
        if latent_dim is None:
            latent_dim = dim
        self.pw1 = nn.Conv1d(
            dim,
            latent_dim * 2,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=bias
        )
        self.use_dw_act = use_dw_act
        self.norm = RMSnorm(latent_dim)
        padding = (kernel_size - 1) // 2
        self.dw = nn.Conv1d(
            latent_dim, latent_dim, kernel_size,
            stride=1,
            padding=padding,
            groups=latent_dim,
            bias=bias
        )
        self.pw2 = nn.Conv1d(
            latent_dim,
            dim,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=bias
        )
        self.out_drop = nn.Dropout(out_drop) if out_drop > 0. else nn.Identity()
        self.latent_drop = nn.Dropout(latent_drop) if latent_drop > 0. else nn.Identity()

    def forward(self, x):
        x = x.transpose(1, 2)
        x = self.pw1(x)
        x = F.gelu(x)
        x1, x2 = x.chunk(2, dim=1)
        x2 = self.norm(x2.transpose(1, 2)).transpose(1, 2)
        x2 = self.dw(x2)
        if self.use_dw_act:
            x2 = F.gelu(x2)
        x = x1 * x2
        x = self.latent_drop(x)
        x = self.pw2(x)
        return self.out_drop(x).transpose(1, 2)


class AttnWROPEX(nn.Module):
    def __init__(
            self, dim, num_heads, head_dim,
            use_rope=True, rope_cache=True,
            dropout_attn: float = 0.0,
            out_drop: float = 0.0
    ):
        super().__init__()

        self.num_heads = num_heads
        attn_dim = head_dim * num_heads
        self.q_linear = nn.Linear(dim, out_features=attn_dim, bias=True)
        self.kv_linear = nn.Linear(dim, out_features=attn_dim * 2, bias=True)

        self.out_linear = nn.Linear(attn_dim, dim, bias=True)
        self.dropout_attn = dropout_attn
        self.out_drop = nn.Dropout(out_drop) if out_drop > 0. else nn.Identity()
        if use_rope:
            self.rope = SingleRoPosEmb(head_dim, use_cache=rope_cache)
        else:
            self.rope = None

    def forward(self, x):

        q = self.q_linear(x)

        k, v = self.kv_linear(x).chunk(2, dim=-1)

        q, k, v = map(lambda value: _to_heads(value, self.num_heads), (q, k, v))
        if self.rope is not None:
            q = self.rope(q)
            k = self.rope(k)

        out = F.scaled_dot_product_attention(
            q, k, v,
            dropout_p=self.dropout_attn,
        )

        out = _from_heads(out)
        out = self.out_linear(out)
        out = self.out_drop(out)
        return out


class PAC(nn.Module):
    def __init__(
            self, dim, num_heads, head_dim,
            c_kernel_size=31, m_kernel_size=31, use_rope=True, rope_cache=True,
            dropout_attn: float = 0.0, out_drop: float = 0.0, c_out_drop=0.1,
            c_latent_drop=0.0,
    ):
        super().__init__()
        self.attn = AttnWROPEX(dim, num_heads, head_dim, use_rope, rope_cache, dropout_attn, out_drop)
        self.c = CgMLP(
            dim, kernel_size=c_kernel_size,
            latent_drop=c_latent_drop, out_drop=c_out_drop
        )

        self.a_norm = RMSnorm(dim)
        self.c_norm = RMSnorm(dim)

        self.merge_linear = nn.Linear(dim * 2, dim)
        self.merge_dw_conv = (
            nn.Conv1d(
                dim * 2, dim * 2, kernel_size=m_kernel_size, stride=1,
                padding=m_kernel_size // 2,
                groups=dim * 2
            )
            if m_kernel_size != 0 else
            None
        )

    def forward(self, x):
        a_o = self.attn(self.a_norm(x))
        c_o = self.c(self.c_norm(x))
        m_o = torch.cat([a_o, c_o], dim=-1)

        if self.merge_dw_conv is not None:
            m_o = self.merge_dw_conv(m_o.transpose(1, 2)).transpose(1, 2) + m_o
        m_o = self.merge_linear(m_o)
        return m_o


class EBF(nn.Module):
    def __init__(
            self, dim, num_heads, head_dim,
            c_kernel_size=31, m_kernel_size=31, use_rope=True, rope_cache=True,
            dropout_attn: float = 0.0, out_drop: float = 0.0, c_out_drop=0.1,
            c_latent_drop=0.0, use_ls=True, ffn_type='glu', ffn_latent_drop=0.1, ffn_out_drop=0.1,
            skip_first_ffn=False, skip_out_ffn=False,
    ):
        super().__init__()
        self.skip_first_ffn = skip_first_ffn
        self.skip_out_ffn = skip_out_ffn

        if ffn_type == 'glu':
            if not skip_first_ffn:
                self.ffn1 = GLUFFN(
                    dim, latent_dim=dim * 4, dropout_latent=ffn_latent_drop,
                    dropout_output=ffn_out_drop
                )
            if not skip_out_ffn:
                self.ffn2 = GLUFFN(
                    dim, latent_dim=dim * 4, dropout_latent=ffn_latent_drop,
                    dropout_output=ffn_out_drop
                )
        elif ffn_type == 'ffn':
            if not skip_first_ffn:
                self.ffn1 = FFN(
                    dim, latent_dim=dim * 4,
                    dropout_latent=ffn_latent_drop,
                    dropout_output=ffn_out_drop
                )
            if not skip_out_ffn:
                self.ffn2 = FFN(
                    dim, latent_dim=dim * 4,
                    dropout_latent=ffn_latent_drop,
                    dropout_output=ffn_out_drop
                )
        elif ffn_type == 'cgmlp':
            if not skip_first_ffn:
                self.ffn1 = CgMLP(
                    dim, latent_dim=int(dim * 2.5), latent_drop=ffn_latent_drop,
                    out_drop=ffn_out_drop, kernel_size=21
                )
            if not skip_out_ffn:
                self.ffn2 = CgMLP(
                    dim, latent_dim=int(dim * 2.5), latent_drop=ffn_latent_drop,
                    out_drop=ffn_out_drop, kernel_size=7
                )
        elif ffn_type == 'eglu':
            if not skip_first_ffn:
                self.ffn1 = HalfCacheGLUFFN(d_model=dim, d_ff=dim * 4, gate_type='silu', quant_bits=0, bias=True)
            if not skip_out_ffn:
                self.ffn2 = HalfCacheGLUFFN(d_model=dim, d_ff=dim * 4, gate_type='silu', quant_bits=0, bias=True)

        else:
            raise ValueError(f"Unknown ffn_type: {ffn_type}")

        self.attn = PAC(
            dim, num_heads, head_dim, c_kernel_size, m_kernel_size, use_rope, rope_cache, dropout_attn,
            out_drop, c_out_drop, c_latent_drop
        )
        if not skip_first_ffn:
            self.norm1 = RMSnorm(dim)
        if not skip_out_ffn:
            self.norm2 = RMSnorm(dim)

        if use_ls:
            if not skip_first_ffn:
                self.lay_scale1 = LayScale(dim)
            self.lay_scale2 = LayScale(dim)
            if not skip_out_ffn:
                self.lay_scale3 = LayScale(dim)
        else:
            if not skip_first_ffn:
                self.lay_scale1 = nn.Identity()
            self.lay_scale2 = nn.Identity()
            if not skip_out_ffn:
                self.lay_scale3 = nn.Identity()

    def forward(self, x, mask=None):
        if not self.skip_first_ffn:
            if mask is not None:
                x = x.masked_fill(~mask.unsqueeze(-1), 0)
            x = self.lay_scale1(self.ffn1(self.norm1(x))) * 0.5 + x
        if mask is not None:
            x = x.masked_fill(~mask.unsqueeze(-1), 0)
        x = self.lay_scale2(self.attn(x)) + x
        if mask is not None:
            x = x.masked_fill(~mask.unsqueeze(-1), 0)
        if not self.skip_out_ffn:
            x = self.lay_scale3(self.ffn2(self.norm2(x))) * 0.5 + x
            if mask is not None:
                x = x.masked_fill(~mask.unsqueeze(-1), 0)
        return x


class EBFBackbone(nn.Module):
    def __init__(
            self, in_dim: int, out_dim: int, return_latent: bool,
            dim: int = 256,
            num_layers: int = 8,
            latent_layer_idx: int = None,
            latent_out_dim: int = 16,
            num_heads: int = 8,
            head_dim: int = 64,
            c_kernel_size: int = 31,
            m_kernel_size: int = 31,
            use_rope: bool = True,
            rope_cache: bool = True,
            dropout_attn: float = 0.0,
            out_drop: float = 0.0,
            c_out_drop: float = 0.1,
            c_latent_drop: float = 0.0,
            use_ls: bool = True,
            ffn_type: str = 'glu',
            ffn_latent_drop: float = 0.1,
            ffn_out_drop: float = 0.1,
            use_out_norm: bool = True,
            skip_first_ffn=False,
            skip_out_ffn=False,
    ):
        super().__init__()
        if is_export_mode():
            rope_cache = False

        self.use_out_norm = use_out_norm
        self.return_latent = return_latent
        if return_latent:
            assert latent_layer_idx <= num_layers
        self.latent_layer_idx = latent_layer_idx

        self.input_proj = nn.Linear(in_dim, dim)

        self.layers = nn.ModuleList([
            EBF(dim=dim, num_heads=num_heads, head_dim=head_dim,
                c_kernel_size=c_kernel_size, m_kernel_size=m_kernel_size,
                use_rope=use_rope, rope_cache=rope_cache,
                dropout_attn=dropout_attn, out_drop=out_drop,
                c_out_drop=c_out_drop, c_latent_drop=c_latent_drop,
                use_ls=use_ls, ffn_type=ffn_type,
                ffn_latent_drop=ffn_latent_drop, ffn_out_drop=ffn_out_drop,
                skip_first_ffn=skip_first_ffn, skip_out_ffn=skip_out_ffn,
                )
            for _ in range(num_layers)
        ])

        if self.return_latent:
            self.latent_norm = RMSnorm(dim)
            self.latent_proj = nn.Linear(dim, latent_out_dim)  # -> [B, T, C_latent]
        if self.use_out_norm:
            self.output_norm = RMSnorm(dim)
        self.output_proj = nn.Linear(dim, out_dim)  # -> [B, T, C_out]

    def forward(self, x, mask=None):
        """
        Args:
            x: [B, T, in_dim] input tensor
            mask: [B, T] valid mask
        Returns:
            latent: [B, T, C_latent] intermediate latent tensor for self cosine similarity
            out: [B, T, C_out] output tensor
        """
        x = self.input_proj(x)

        latent = None
        for i, layer in enumerate(self.layers):
            x = layer(x, mask=mask)
            if self.return_latent and i == self.latent_layer_idx - 1:
                latent = self.latent_norm(x)
                latent = self.latent_proj(latent)  # [B, T, C_latent]

        if self.use_out_norm:
            x = self.output_norm(x)
        out = self.output_proj(x)  # [B, T, C_out]

        if self.return_latent:
            return out, latent
        else:
            return out
