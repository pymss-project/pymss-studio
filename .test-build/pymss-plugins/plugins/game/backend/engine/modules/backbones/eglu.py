import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional, Tuple
from enum import Enum

try:
    from torch.amp import custom_fwd as _custom_fwd, custom_bwd as _custom_bwd

    custom_fwd = _custom_fwd(device_type="cuda")
    custom_bwd = _custom_bwd(device_type="cuda")
except TypeError:
    from torch.amp import custom_fwd, custom_bwd
except ImportError:
    from torch.cuda.amp import custom_fwd, custom_bwd


class GateType(Enum):
    SILU = "silu"
    SIGMOID = "sigmoid"
    GELU = "gelu"


class _HalfCacheGLUFunction(torch.autograd.Function):
    """
    缓存清单（forward 结束后留存）：
      - x          (bf16)   [B, S, D]
      - gate       (bf16)   [B, S, D_ff]
      - hidden_q   (int8)   [B, S, D_ff]    ← 关键：比 bf16 小 2x
      - hidden_scale (fp32) [B, S, 1]

    不缓存（反向恢复）：
      - gate_act   → 从 gate 重算
      - up         → hidden / gate_act
    """

    @staticmethod
    @custom_fwd
    def forward(
            ctx,
            x: torch.Tensor,
            W_gate: torch.Tensor,
            W_up: torch.Tensor,
            W_down: torch.Tensor,
            gate_type: str = "silu",
            eps: float = 1e-6,
            quant_bits: int = 8,
            bias_gate: Optional[torch.Tensor] = None,
            bias_up: Optional[torch.Tensor] = None,
            bias_down: Optional[torch.Tensor] = None,
    ):
        # 分步计算，每步完成后立即释放不需要的中间张量
        gate = F.linear(x, W_gate, bias_gate)  # [B, S, D_ff] bf16

        # 计算 up，然后立即与 gate_act 合并成 hidden
        up = F.linear(x, W_up, bias_up)  # [B, S, D_ff] bf16
        gate_act = _gate_fn(gate, gate_type)  # [B, S, D_ff] bf16
        hidden = gate_act * up  # [B, S, D_ff] bf16

        # gate_act 和 up 不再需要，立即释放
        del gate_act, up

        # 量化 hidden → int8（内存减半）
        if quant_bits == 8:
            hidden_q, hidden_scale = _quantize_int8(hidden)
        else:
            hidden_q = hidden
            hidden_scale = None

        # 计算输出
        out = F.linear(hidden, W_down, bias_down)  # [B, S, D] bf16

        # hidden(bf16) 不再需要，只保留 hidden_q(int8)
        del hidden

        # 只保存 activation 张量
        ctx.save_for_backward(x, gate, hidden_q, hidden_scale)

        # 权重通过普通属性引用（不增加 activation 内存）
        ctx.W_gate = W_gate
        ctx.W_up = W_up
        ctx.W_down = W_down
        ctx.bias_gate = bias_gate
        ctx.bias_up = bias_up
        ctx.bias_down = bias_down
        ctx.gate_type = gate_type
        ctx.eps = eps
        ctx.quant_bits = quant_bits

        return out

    @staticmethod
    @custom_bwd
    def backward(ctx, grad_out: torch.Tensor):
        x, gate, hidden_q, hidden_scale = ctx.saved_tensors

        W_gate = ctx.W_gate
        W_up = ctx.W_up
        W_down = ctx.W_down
        bias_gate = ctx.bias_gate
        bias_up = ctx.bias_up
        bias_down = ctx.bias_down
        gate_type = ctx.gate_type
        eps = ctx.eps
        dtype = x.dtype

        # 反量化 hidden
        if ctx.quant_bits == 8:
            hidden = _dequantize_int8(hidden_q, hidden_scale, dtype)
        else:
            hidden = hidden_q
        del hidden_q, hidden_scale

        # 重算 gate_act
        gate_act = _gate_fn(gate, gate_type)

        # 除法恢复 up
        up = _safe_div_recover(hidden, gate_act, eps)

        # --- W_down 反向 ---
        grad_hidden = grad_out @ W_down
        grad_W_down = grad_out.reshape(-1, grad_out.shape[-1]).T @ hidden.reshape(-1, hidden.shape[-1])
        grad_bias_down = grad_out.sum(dim=(0, 1)) if bias_down is not None else None
        del hidden

        # --- 乘法节点反向 ---
        grad_gate_act = grad_hidden * up
        grad_up = grad_hidden * gate_act
        del grad_hidden, up

        # --- 门控激活函数反向 ---
        grad_gate = grad_gate_act * _gate_fn_backward(gate, gate_act, gate_type)
        del grad_gate_act, gate_act, gate

        # --- W_gate / W_up 反向 ---
        x_2d = x.reshape(-1, x.shape[-1])
        grad_gate_2d = grad_gate.reshape(-1, grad_gate.shape[-1])
        grad_up_2d = grad_up.reshape(-1, grad_up.shape[-1])

        grad_x = grad_gate @ W_gate + grad_up @ W_up
        grad_W_gate = grad_gate_2d.T @ x_2d
        grad_W_up = grad_up_2d.T @ x_2d

        grad_bias_gate = grad_gate.sum(dim=(0, 1)) if bias_gate is not None else None
        grad_bias_up = grad_up.sum(dim=(0, 1)) if bias_up is not None else None

        return (grad_x, grad_W_gate, grad_W_up, grad_W_down,
                None, None, None,
                grad_bias_gate, grad_bias_up, grad_bias_down)


def _gate_fn(gate: torch.Tensor, gate_type: str) -> torch.Tensor:
    if gate_type == "silu":
        return F.silu(gate)
    elif gate_type == "sigmoid":
        return torch.sigmoid(gate)
    elif gate_type == "gelu":
        return F.gelu(gate)
    else:
        raise ValueError(f"Unknown gate type: {gate_type}")


def _gate_fn_backward(
        gate: torch.Tensor,
        gate_act: torch.Tensor,
        gate_type: str,
) -> torch.Tensor:
    if gate_type == "silu":
        sig = torch.sigmoid(gate)
        return gate_act + sig * (1.0 - gate_act)
    elif gate_type == "sigmoid":
        return gate_act * (1.0 - gate_act)
    elif gate_type == "gelu":
        inv_sqrt2 = 0.7071067811865476
        cdf = 0.5 * (1.0 + torch.erf(gate * inv_sqrt2))
        pdf = torch.exp(-0.5 * gate * gate) * 0.3989422804014327
        return cdf + gate * pdf
    else:
        raise ValueError(f"Unknown gate type: {gate_type}")


def _safe_div_recover(
        hidden: torch.Tensor,
        gate_act: torch.Tensor,
        eps: float,
) -> torch.Tensor:
    sign = gate_act.sign()
    sign[sign == 0] = 1.0
    safe_denom = gate_act.abs().clamp(min=eps) * sign
    return hidden / safe_denom


def _quantize_int8(
        tensor: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor]:
    shape = tensor.shape
    flat = tensor.reshape(-1, shape[-1])
    flat_f32 = flat.float()
    scale = flat_f32.abs().amax(dim=-1, keepdim=True).clamp(min=1e-12) / 127.0
    quantized = (flat_f32 / scale).round().clamp(-128, 127).to(torch.int8)
    return quantized.reshape(shape), scale.reshape(*shape[:-1], 1)


def _dequantize_int8(
        quantized: torch.Tensor,
        scale: torch.Tensor,
        dtype: torch.dtype,
) -> torch.Tensor:
    return quantized.to(dtype) * scale.to(dtype)


class HalfCacheGLUFFN(nn.Module):
    def __init__(
            self,
            d_model: int,
            d_ff: int,
            gate_type: str = "silu",
            bias: bool = False,
            eps: float = 1e-6,
            quant_bits: int = 8,
            device: Optional[torch.device] = None,
            dtype: Optional[torch.dtype] = None,
    ):
        super().__init__()
        factory = {"device": device, "dtype": dtype}

        self.d_model = d_model
        self.d_ff = d_ff
        self.gate_type = gate_type
        self.eps = eps
        self.quant_bits = quant_bits

        self.W_gate = nn.Parameter(torch.empty(d_ff, d_model, **factory))
        self.W_up = nn.Parameter(torch.empty(d_ff, d_model, **factory))
        self.W_down = nn.Parameter(torch.empty(d_model, d_ff, **factory))

        if bias:
            self.bias_gate = nn.Parameter(torch.zeros(d_ff, **factory))
            self.bias_up = nn.Parameter(torch.zeros(d_ff, **factory))
            self.bias_down = nn.Parameter(torch.zeros(d_model, **factory))
        else:
            self.register_parameter("bias_gate", None)
            self.register_parameter("bias_up", None)
            self.register_parameter("bias_down", None)

        self.reset_parameters()

    def reset_parameters(self):
        nn.init.kaiming_uniform_(self.W_gate)
        nn.init.kaiming_uniform_(self.W_up)
        nn.init.kaiming_uniform_(self.W_down)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.training:
            return _HalfCacheGLUFunction.apply(
                x,
                self.W_gate, self.W_up, self.W_down,
                self.gate_type, self.eps, self.quant_bits,
                self.bias_gate, self.bias_up, self.bias_down,
            )
        else:
            gate = F.linear(x, self.W_gate, self.bias_gate)
            up = F.linear(x, self.W_up, self.bias_up)
            hidden = _gate_fn(gate, self.gate_type) * up
            return F.linear(hidden, self.W_down, self.bias_down)

    def extra_repr(self) -> str:
        return (f"d_model={self.d_model}, d_ff={self.d_ff}, "
                f"gate_type={self.gate_type}, quant_bits={self.quant_bits}, "
                f"bias={self.bias_gate is not None}")
