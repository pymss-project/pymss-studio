"""Minimum GPU activation checks in a disposable, tree-supervised process."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from worker_protocol import _redact_sensitive_text
from worker_runtime_inventory import _child, system_inventory
from worker_runtime_paths import discover_rocm_native_paths

_CHECK = r'''
import ctypes, json, re, sys
targets, indices, selection = [json.loads(value) for value in sys.argv[1:4]]
result = {'status': 'failed', 'devices': []}
try:
 import torch
 if not getattr(torch.version, 'hip', None) or not torch.cuda.is_available():
  raise RuntimeError('No usable ROCm GPU is available')
 count = torch.cuda.device_count()
 pci_fn = None
 if selection['hipLibrary']:
  library = ctypes.CDLL(selection['hipLibrary'])
  pci_fn = library.hipDeviceGetPCIBusId
  pci_fn.argtypes = [ctypes.POINTER(ctypes.c_char), ctypes.c_int, ctypes.c_int]
  pci_fn.restype = ctypes.c_int
 requested = set(selection['ids']); found = set()
 for index in (indices if indices is not None else list(range(min(count, 256)))):
  item = {'index': index, 'status': 'failed'}
  try:
   prop = torch.cuda.get_device_properties(index)
   raw = str(getattr(prop, 'gcnArchName', '')).lower()
   match = re.fullmatch(r'(gfx[0-9a-f]{3,6})(?::[^\s]{0,100})?', raw)
   gfx = match.group(1) if match else None
   stable = None
   if pci_fn is not None:
    bus = ctypes.create_string_buffer(64)
    if pci_fn(bus, len(bus), index) == 0:
     pci = bus.value.decode('ascii').lower()
     if re.fullmatch(r'[0-9a-f]{4,8}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]', pci):
      stable = 'hip-pci:' + pci
   matches = {stable} & requested if stable else set()
   if selection['uniquePnp'] and count == 1:
    matches |= requested
   if requested and not matches:
    continue
   found |= matches
   item.update({'name': str(prop.name)[:160], 'gfx': gfx, 'stableId': stable})
   if targets and gfx not in targets:
    item['status'] = 'targetMissing'
   else:
    source = torch.ones(16, device='cuda:' + str(index), dtype=torch.float32)
    actual = (source + 1).sum()
    torch.cuda.synchronize(index)
    if actual.item() != 32:
     raise RuntimeError('The minimum GPU kernel returned an incorrect result')
    item['status'] = 'passed'
  except Exception as error:
   item['message'] = type(error).__name__ + ': ' + str(error).splitlines()[0][:300]
  result['devices'].append(item)
 all_required = bool(requested) or indices is not None
 passed = bool(result['devices']) and (all(d['status'] == 'passed' for d in result['devices']) if all_required else any(d['status'] == 'passed' for d in result['devices']))
 if requested and found != requested:
  result['reason'] = 'selectedDeviceMappingUnavailable'
  passed = False
 result['status'] = 'passed' if passed else 'failed'
except Exception as error:
 result['message'] = type(error).__name__ + ': ' + str(error).splitlines()[0][:300]
print(json.dumps(result))
'''


def _runtime_root(python: Path) -> Path:
    parent = python.parent.resolve()
    return parent.parent if parent.name.lower() in {"scripts", "bin"} else parent


def verify_activation(python: Path, targets: list[str], device_ids: Any = None,
                      *, selected_device_ids: Any = None, environment: dict[str, str] | None = None) -> dict[str, Any]:
    import json
    if device_ids is not None and (not isinstance(device_ids, list) or not 1 <= len(device_ids) <= 8
            or any(type(index) is not int or not 0 <= index <= 255 for index in device_ids)):
        raise ValueError("Runtime device IDs must contain between one and eight nonnegative indices")
    selection = [] if selected_device_ids is None else selected_device_ids
    if (not isinstance(selection, list) or len(selection) > 8
            or any(not isinstance(value, str) or len(value) > 160
                   or not re.fullmatch(r'(hip-pci:[0-9a-f:.]+|pnp:[0-9a-f]{24})', value) for value in selection)):
        raise ValueError("Runtime hardware selection must use confirmed persistent device identities")
    root = _runtime_root(python)
    library = None
    unique_pnp = False
    try:
        if any(value.startswith("hip-pci:") for value in selection):
            paths = discover_rocm_native_paths(root, python)
            candidates = {path.resolve() for raw in paths["nativeLibraryDirs"]
                          for path in (root / raw).glob("amdhip64*.dll")}
            if len(candidates) != 1 or not next(iter(candidates)).is_relative_to(root):
                raise ValueError("The selected runtime does not expose an unambiguous HIP library")
            library = str(next(iter(candidates)))
        if selection and all(value.startswith("pnp:") for value in selection):
            adapters = [device for device in system_inventory() if device["vendor"] == "amd"]
            unique_pnp = len(selection) == 1 and len(adapters) == 1 and adapters[0]["id"] == selection[0]
        request = {"ids": selection, "hipLibrary": library, "uniquePnp": unique_pnp}
        result = _child([str(python), "-I", "-B", "-c", _CHECK, json.dumps(targets), json.dumps(device_ids), json.dumps(request)],
                        timeout=60, environment=environment, last_json=True)
        if result.get("status") not in {"passed", "failed"} or not isinstance(result.get("devices"), list):
            raise ValueError("The GPU activation check returned an invalid result")
        if "message" in result:
            result["message"] = _redact_sensitive_text(str(result["message"]))[:500]
        for device in result["devices"]:
            if "message" in device:
                device["message"] = _redact_sensitive_text(str(device["message"]))[:500]
        return result
    except subprocess.TimeoutExpired:
        return {"status": "failed", "reason": "timeout", "devices": []}
    except (OSError, ValueError, RuntimeError, IndexError):
        return {"status": "failed", "reason": "processFailed", "devices": []}
