"""Check outputs produced by the GUI without changing them or installing plugins."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--kit', type=Path, default=Path(__file__).parent)
    args = parser.parse_args()
    kit = args.kit.resolve()
    work = Path((kit / 'last-gui-workspace.txt').read_text(encoding='utf-8').strip()).resolve()
    if work.parent != kit or not work.name.startswith('gui-'):
        raise ValueError('The GUI workspace must be a generated directory in this test kit')
    build = json.loads((kit / 'build-info.json').read_text(encoding='utf-8'))
    if json.loads((work / 'workspace.json').read_text(encoding='utf-8'))['build'] != build:
        raise ValueError('GUI fixtures belong to another build')
    resource = args.app.resolve() / 'Contents/Resources'
    if not (resource / 'python/worker.py').is_file():
        resource /= 'resources'
    if json.loads((resource / 'python/plugin-test-build.json').read_text(encoding='utf-8')) != build:
        raise ValueError('The application and test kit have different build identifiers')
    checks = []
    expected = {(work / 'expected/LF.txt').read_bytes(), (work / 'expected/CRLF.txt').read_bytes()}
    for name in ['python', 'rust', 'workflow']:
        files = sorted((work / 'outputs' / name).glob('*.txt'))
        small = [path for path in files[:128] if path.is_file() and not path.is_symlink() and path.stat().st_size <= 1024]
        values = {path.read_bytes() for path in small}
        checks.append({'check': name + '_lf_crlf', 'passed': expected <= values,
            'files': [{'name': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in small]})
    probes = []
    for path in sorted((work / 'outputs/audio').rglob('*'))[:128]:
        if path.suffix.lower() not in {'.wav', '.flac'} or not path.is_file() or path.is_symlink():
            continue
        result = subprocess.run([str(resource / 'bin/ffprobe'), '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
            capture_output=True, text=True, timeout=30)
        value = json.loads(result.stdout) if result.returncode == 0 else {}
        streams = [item for item in value.get('streams', []) if item.get('codec_type') == 'audio']
        stream = streams[0] if streams else {}
        duration = float(value.get('format', {}).get('duration', 0))
        passed = stream.get('channels') == 2 and str(stream.get('sample_rate')) == '48000' and abs(duration - 12) < 0.02
        probes.append({'name': str(path.relative_to(work)), 'passed': passed, 'probe': value})
    checks.append({'check': 'audio_roundtrip', 'passed': len(probes) >= 2 and all(item['passed'] for item in probes), 'files': probes})
    signature = subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(args.app.resolve())], capture_output=True, text=True, timeout=90)
    checks.append({'check': 'application_signature', 'passed': signature.returncode == 0, 'detail': signature.stderr[-2000:]})
    report = {'status': 'passed' if all(item['passed'] for item in checks) else 'failed_or_incomplete',
        'build': build, 'checks': checks, 'manualChecks': 'Record theme, animation, installation and cancellation results in MANUAL-RESULTS.md'}
    destination = work / ('gui-results-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.json')
    with destination.open('x', encoding='utf-8') as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    for item in checks:
        print('PASS' if item['passed'] else 'FAIL / NOT COMPLETED', item['check'])
    print('Report:', destination)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
