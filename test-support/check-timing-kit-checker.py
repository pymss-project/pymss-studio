"""Exercise report success/failure using synthetic outputs, never counted as GUI acceptance."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

import numpy as np
import soundfile as sf

kit, app = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location("timing_acceptance", kit / "acceptance.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with tempfile.TemporaryDirectory(prefix="timing-checker-selftest-") as temporary:
    root = Path(temporary)
    module.ROOT = root
    module.GUI = root / "gui"
    module.REPORTS = root / "reports"
    module.prepare(app)
    manifest = json.loads((root / "fixtures.json").read_text())
    by_path = {item["path"]: item for item in manifest["inputs"]}
    for case in manifest["cases"]:
        source = by_path[case["input"]]
        frames = source["frames"] * case["durationPercent"] // 100
        path = Path(case["outputDirectory"]) / (Path(case["input"]).stem + "_processed_2.wav")
        samples = np.ones((frames, 2), dtype=np.float32) * 0.01
        sf.write(path, samples, source["sampleRate"])
        if case["id"] == "repeat":
            sf.write(path.with_name(path.stem + "_again.wav"), samples, source["sampleRate"])
    state_path = module.GUI / "runtime-envs/active-runtime.json"
    module.write_json(state_path, {"pymssVersion": "2.3.2", "pymssCoreVersion": "0.1.14", "torchVersion": "2.7.1",
                                   "backend": "mlx", "packages": {"mlx": True}})
    module.check(app)
    # A wrong sample rate must fail, while still producing a portable diagnostic archive.
    case = manifest["cases"][1]
    output = next(Path(case["outputDirectory"]).glob("*.wav"))
    sf.write(output, np.ones((48000, 2)) * 0.01, 44100)
    try:
        module.check(app)
    except RuntimeError:
        pass
    else:
        raise AssertionError("The result checker accepted a wrong-rate/wrong-duration output")
print("Checker synthetic selftest PASS; manual GUI acceptance remains NOT RUN")
