from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class SeparatorCase:
    name: str
    model_type: str
    model_path: Path
    config_path: Path
    input_path: Path
    device: str
    store_dirs: str | Mapping[str, str | None]
    output_format: str = "wav"
    device_ids: tuple[int, ...] = (0,)

    def missing_paths(self) -> list[Path]:
        return [path for path in (self.model_path, self.config_path, self.input_path) if not path.exists()]


SEPARATOR_CASES = [
    SeparatorCase(
        name="htdemucs_6stems",
        model_type="htdemucs",
        model_path=Path("pretrain/multi_stem_models/HTDemucs4_6stems.th"),
        config_path=Path("configs/multi_stem_models/config_htdemucs_6stems.yaml"),
        input_path=Path("input"),
        device="cpu",
        store_dirs={
            "vocals": "./output/vocals",
            "other": None,
        },
    ),
    SeparatorCase(
        name="bandit_dnr",
        model_type="bandit",
        model_path=Path("pretrain/multi_stem_models/model_bandit_plus_dnr_sdr_11.47.chpt"),
        config_path=Path("configs/multi_stem_models/config_dnr_bandit_bsrnn_multi_mus64.yaml"),
        input_path=Path("input"),
        device="cpu",
        store_dirs={
            "speech": "./output/speech",
            "effects": None,
            "music": "./output/music",
        },
    ),
    SeparatorCase(
        name="mdx23c_musdb18",
        model_type="mdx23c",
        model_path=Path("pretrain/multi_stem_models/model_mdx23c_ep_168_sdr_7.0207.ckpt"),
        config_path=Path("configs/multi_stem_models/config_musdb18_mdx23c.yaml"),
        input_path=Path("input"),
        device="mps",
        store_dirs={
            "vocals": "./output/vocals",
            "other": "./output/other",
        },
    ),
    SeparatorCase(
        name="deverb_bs_roformer",
        model_type="bs_roformer",
        model_path=Path("pretrain/single_stem_models/deverb_bs_roformer_8_256dim_8depth.ckpt"),
        config_path=Path("configs/single_stem_models/deverb_bs_roformer_8_256dim_8depth.yaml"),
        input_path=Path("input"),
        device="mps",
        store_dirs="output",
    ),
    SeparatorCase(
        name="apollo_lq_mp3_restoration",
        model_type="apollo",
        model_path=Path("pretrain/single_stem_models/Apollo_LQ_MP3_restoration.ckpt"),
        config_path=Path("configs/single_stem_models/config_apollo.yaml"),
        input_path=Path("mp3"),
        device="mps",
        store_dirs="output",
    ),
    SeparatorCase(
        name="vocal_bs_roformer",
        model_type="bs_roformer",
        model_path=Path("pretrain/vocal_models/model_bs_roformer_ep_317_sdr_12.9755.ckpt"),
        config_path=Path("configs/vocal_models/model_bs_roformer_ep_317_sdr_12.9755.yaml"),
        input_path=Path("input"),
        device="mps",
        store_dirs="output",
    ),
    SeparatorCase(
        name="scnet_musdb18",
        model_type="scnet",
        model_path=Path("pretrain/multi_stem_models/scnet_checkpoint_musdb18.ckpt"),
        config_path=Path("configs/multi_stem_models/config_musdb18_scnet.yaml"),
        input_path=Path("input"),
        device="mps",
        store_dirs="output",
    ),
    SeparatorCase(
        name="mel_band_roformer_vocals",
        model_type="mel_band_roformer",
        model_path=Path("pretrain/vocal_models/mel_band_roformer_vocals_becruily.ckpt"),
        config_path=Path("configs/vocal_models/config_vocals_becruily.yaml"),
        input_path=Path("input"),
        device="cpu",
        store_dirs={
            "vocals": "./output/vocals",
        },
    ),
]
