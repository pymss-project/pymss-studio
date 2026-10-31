"""Verify workflow progress and resource cleanup using installed CUDA models."""
from __future__ import annotations

import argparse
import gc
import json
import time
import traceback
import weakref
from pathlib import Path
from unittest import mock

import numpy as np
import torch
import pymss
from pymss import MSSeparator, load_audio, save_audio
import pymss.graph as graph



def node(node_id, node_type, *, inputs=(), outputs=(), widgets=()):
    return {"id": node_id, "type": node_type, "inputs": list(inputs), "outputs": list(outputs),
            "widgets_values": list(widgets)}


def audio_input(link):
    return {"name": "audio", "type": "AUDIO", "link": link}


def audio_output(stem, links):
    return {"name": stem, "type": "AUDIO", "links": links}


def separation(node_id, model, *, vr, audio_link, output_link, stem, params_link=None):
    inputs = [audio_input(audio_link)]
    if params_link is not None:
        inputs.append({"name": "params", "type": "PYMSS_MSS_PARAMS", "link": params_link})
    return node(node_id, "vr_separate" if vr else "mss_separate", inputs=inputs,
                outputs=[audio_output(f"{stem} (Audio)", [output_link])],
                widgets=[model, "cuda", False, "modelscope", "0", False])


def save_node(node_id, link):
    return node(node_id, "pymss_save_audio", inputs=[audio_input(link)],
                widgets=["wav", "44100", "FLOAT", "PCM_24", "320k"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()
    root = Path(args.output_root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = root / "inputs"
    source.mkdir()
    sample_rate = 44100
    rng = np.random.default_rng(147)
    sources = []
    for index, duration in enumerate((2, 2.5, 3)):
        t = np.arange(int(sample_rate * duration), dtype=np.float32) / sample_rate
        mono = 0.12 * np.sin(2 * np.pi * (220 + index * 30) * t) + 0.03 * rng.standard_normal(t.shape).astype(np.float32)
        audio = np.column_stack([mono, mono * 0.9])
        path = source / f"clip_{index + 1}.wav"
        save_audio(str(path), audio, sample_rate, "wav", {"wav_bit_depth": "FLOAT"})
        sources.append(path)

    batch = node(1, "pymss_load_audio_batch", outputs=[audio_output("audio", [1])],
                 widgets=[str(source), False, True, ""])
    vr = separation(2, "1_HP-UVR.pth", vr=True, audio_link=1, output_link=2, stem="Instrumental")
    batch_links = [[1, 1, 0, 2, 0, "AUDIO"], [2, 2, 0, 3, 0, "AUDIO"]]
    vr_list = {**vr, "type": "vr_separate_list", "outputs": [
        audio_output("audios", [2]), {"name": "stem_names", "type": "STRING", "links": None},
    ]}
    cases = {
        "vr_batch_plain": {"version": 0.4, "nodes": [batch, vr, save_node(3, 2)], "links": batch_links},
        "vr_batch_list": {"version": 0.4, "nodes": [batch, vr_list, save_node(3, 2)], "links": batch_links},
        "model_switch": {"version": 0.4, "nodes": [
            node(1, "input_audio", outputs=[audio_output("audio", [1])]),
            separation(2, "1_HP-UVR.pth", vr=True, audio_link=1, output_link=2, stem="Instrumental"),
            separation(3, "3_HP-Vocal-UVR.pth", vr=True, audio_link=2, output_link=3, stem="Vocals"),
            separation(4, "bs_karaoke_gabox_IS.ckpt", vr=False, audio_link=3, output_link=4,
                       stem="vocals", params_link=5),
            save_node(5, 4),
            node(6, "pymss_mss_params", outputs=[{"name": "params", "type": "PYMSS_MSS_PARAMS", "links": [5]}],
                 widgets=[1, "Default", "Default", False, False, False]),
        ], "links": [[1, 1, 0, 2, 0, "AUDIO"], [2, 2, 0, 3, 0, "AUDIO"],
                     [3, 3, 0, 4, 0, "AUDIO"], [4, 4, 0, 5, 0, "AUDIO"], [5, 6, 0, 4, 1, "PYMSS_MSS_PARAMS"]]},
    }
    cases["model_switch_repeat"] = cases["model_switch"]
    cases["failure_after_model"] = {"version": 0.4, "nodes": [
        node(1, "input_audio", outputs=[audio_output("audio", [1])]),
        separation(2, "1_HP-UVR.pth", vr=True, audio_link=1, output_link=2, stem="Instrumental"),
        node(3, "unavailable_processor", inputs=[audio_input(2)]),
    ], "links": [[1, 1, 0, 2, 0, "AUDIO"], [2, 2, 0, 3, 0, "AUDIO"]]}
    torch.cuda.init()
    original_factory = graph.SeparatorCache._default_factory
    original_close = MSSeparator.close
    original_separate = MSSeparator.separate
    report = {"pymss_path": pymss.__file__, "torch": torch.__version__, "gpu": torch.cuda.get_device_name(), "cases": {}}
    failed = False
    for name, definition in cases.items():
        print(f"CASE_START {name}", flush=True)
        loaded = []
        events = []
        progress = []
        calls = []
        errors = []
        gpu_tensor_refs = []
        baseline = torch.cuda.memory_allocated()
        torch.cuda.reset_peak_memory_stats()
        started = time.monotonic()

        def factory(**kwargs):
            assert not any(separator.model is not None for separator in loaded), "Previous model is still resident"
            assert not any(ref() is not None and ref().is_cuda for ref in gpu_tensor_refs), "Previous model tensors are still on CUDA"
            events.append({"event": "load_start", "model": kwargs.get("model_name"), "gpu_bytes": torch.cuda.memory_allocated()})
            separator = original_factory(**kwargs)
            loaded.append(separator)
            events.append({"event": "load_done", "model": kwargs.get("model_name"), "gpu_bytes": torch.cuda.memory_allocated()})
            return separator

        def close(separator):
            model = getattr(separator, "model", None)
            model = getattr(model, "model_run", None) if separator.model_type == "vr" else model
            if isinstance(model, torch.nn.Module):
                gpu_tensor_refs.extend(weakref.ref(tensor) for tensor in (*model.parameters(), *model.buffers()) if tensor.is_cuda)
            original_close(separator)
            events.append({"event": "closed", "gpu_bytes": torch.cuda.memory_allocated()})

        def separate(separator, mix, **kwargs):
            calls.append({"model_type": separator.model_type, "samples": int(mix.shape[-1])})
            return original_separate(separator, mix, **kwargs)

        def emit(payload):
            progress.append(payload)
            if "completed" in payload.get("message", ""):
                print(f"AUDIO_COMPLETED {name} {payload['overall_fraction']:.3f}", flush=True)

        entry = {}
        try:
            with mock.patch.object(graph.SeparatorCache, "_default_factory", side_effect=factory),                     mock.patch.object(MSSeparator, "close", new=close),                     mock.patch.object(MSSeparator, "separate", new=separate):
                input_path = None if name.startswith("vr_batch") else str(sources[0])
                dag = graph.load_comfy_graph(definition)
                try:
                    files = graph.run_dag(dag, output_dir=root / name, input_path=input_path,
                                          device="cuda", model_dir=args.model_dir, download=False,
                                          output_format="wav", progress_event_callback=emit)
                except graph.UnknownNodeError as error:
                    if name != "failure_after_model":
                        raise
                    errors.append({"code": "UNKNOWN_NODE", "message": str(error)})
                    files = []
                if name == "failure_after_model":
                    assert errors and len(loaded) == len(calls) == 1
                result = {"files": files}
            percentages = [item["overall_fraction"] for item in progress]
            assert percentages == sorted(percentages), "Workflow progress moved backwards"
            if name != "failure_after_model":
                assert percentages[-1] == 1
            assert all(separator.model is None for separator in loaded), "Model was not closed"
            gc.collect()
            torch.cuda.synchronize()
            assert not any(ref() is not None and ref().is_cuda for ref in gpu_tensor_refs), "Model parameters or buffers remain on CUDA"
            entry["model_gpu_tensors_released"] = True
            entry["native_final_gpu_bytes"] = torch.cuda.memory_allocated()
            # These process-wide CUDA workspaces are distinct from model weights.
            clear_workspaces = getattr(torch._C, "_cuda_clearCublasWorkspaces", None)
            if callable(clear_workspaces):
                clear_workspaces()
            torch.backends.cuda.cufft_plan_cache.clear()
            torch.cuda.empty_cache()
            entry["after_workspace_clear_gpu_bytes"] = torch.cuda.memory_allocated()
            expected = 0 if name == "failure_after_model" else 6 if name == "vr_batch_list" else 1
            assert len(result["files"]) == expected
            outputs = []
            for path in result["files"]:
                audio, sr = load_audio(path, sr=None, mono=False)
                assert int(sr) == sample_rate and np.isfinite(audio).all()
                duration = audio.shape[-1] / sr
                assert 1.8 <= duration <= 3.2, f"Unexpected audio duration {duration}"
                outputs.append({"path": path, "duration": duration, "channels": int(audio.shape[0])})
            if name.startswith("vr_batch"):
                completed = [item["overall_fraction"] for item in progress if "completed" in item.get("message", "")]
                assert len(calls) == 3 and len(loaded) == 1
                assert len(completed) == 3 and completed[0] < completed[1] < completed[2]
            elif name.startswith("model_switch"):
                assert len(loaded) == len(calls) == 3
                if name.endswith("repeat"):
                    assert entry["native_final_gpu_bytes"] == report["cases"]["model_switch"]["native_final_gpu_bytes"]
            entry.update(ok=True, outputs=outputs)
        except Exception as error:
            failed = True
            entry.update(ok=False, error=str(error), traceback=traceback.format_exc())
            print(f"CASE_FAILED {name}: {error}", flush=True)
        finally:
            entry.update(seconds=round(time.monotonic() - started, 3), calls=calls, events=events, errors=errors,
                         progress=progress, peak_gpu_bytes=torch.cuda.max_memory_allocated(),
                         final_gpu_bytes=torch.cuda.memory_allocated(), baseline_gpu_bytes=baseline)
            report["cases"][name] = entry
            (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"CASE_DONE {name} ok={entry['ok']} seconds={entry['seconds']} peak_gpu_mb={entry['peak_gpu_bytes'] // 2**20}", flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
