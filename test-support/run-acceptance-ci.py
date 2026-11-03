from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time

app, plugins, core, report = (Path(value).resolve() for value in sys.argv[1:])
studio = Path(__file__).resolve().parents[1]
resources = next(path for path in [app / 'Contents/Resources', app / 'Contents/Resources/resources']
                 if (path / 'python/worker.py').is_file())
python = resources / 'python-runtime/bin/python3'
marker = json.loads((resources / 'python/timing-test-build.json').read_text(encoding='utf-8'))
results = {'build': marker, 'commit': os.environ['GITHUB_SHA'], 'runId': os.environ['GITHUB_RUN_ID'],
           'runAttempt': os.environ['GITHUB_RUN_ATTEMPT'], 'checks': []}

with tempfile.TemporaryDirectory(prefix='timing-build-qa-') as temporary:
    root = Path(temporary)
    env = os.environ.copy()
    env.update(PYTHONHOME=str(resources / 'python-runtime'), PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1',
               PYMSS_STUDIO_DATA_ROOT=str(root / 'data'), PYMSS_USER_MODELS=str(root / 'user-models.json'),
               PYMSS_MODEL_DIR=str(root / 'models'), NUMBA_CACHE_DIR=str(root / 'numba'),
               RADIUS_FFMPEG=str(resources / 'bin/ffmpeg'), RADIUS_FFMPEG_IO='1')
    env.pop('PYMSS_PLUGIN_BINDING', None)
    env['PATH'] = str(resources / 'bin') + os.pathsep + env['PATH']

    def run(name, arguments, *, cwd=studio, paths=None):
        current = dict(env)
        current['PYTHONPATH'] = os.pathsep.join(map(str, paths or []))
        started = time.time()
        result = subprocess.run([str(python), '-B', '-X', 'utf8', *arguments], cwd=cwd, env=current,
                                text=True, capture_output=True, timeout=360)
        print(name, result.stdout[-2000:], result.stderr[-4000:], flush=True)
        results['checks'].append({'name': name, 'exitCode': result.returncode,
                                  'seconds': round(time.time() - started, 2),
                                  'stdout': result.stdout[-3000:], 'stderr': result.stderr[-5000:]})
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding='utf-8')
        if result.returncode:
            raise SystemExit(f'{name} failed with {result.returncode}')

    run('bundled baseline identity', ['-c',
        "import importlib.metadata as m; assert m.version('pymss')=='2.3.1'; assert m.version('pymss-core')=='0.1.14'; print('pymss 2.3.1 / core 0.1.14')"])
    run('Studio core update contracts', ['-m', 'unittest', 'tests.python.test_runtime_core_plan',
        'tests.python.test_runtime_core_update'], paths=[studio / 'python', studio])
    run('Timing operations and native runtimes on baseline', ['-m', 'unittest',
        'tooling.tests.test_timing_file_operation', 'tooling.tests.test_timing_radius',
        'tooling.tests.test_timing_rubberband', 'tooling.tests.test_timing_runtime',
        'tooling.tests.test_timing_progress', 'tooling.tests.test_tool_plugins'], cwd=plugins, paths=[plugins])
    dependencies = root / 'test-dependencies'
    run('isolated pytest dependency preparation', ['-m', 'pip', 'install', '--disable-pip-version-check',
        '--target', str(dependencies), 'pytest>=8,<9'])
    run('core source installation contracts', ['-m', 'pytest', '-q', 'test/test_plugin_sources.py',
        'test/test_plugin_macos_attributes.py', 'test/test_plugin_marketplace.py'], cwd=core, paths=[core, dependencies])

subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
results['signatureAfterTests'] = 'PASS'
report.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding='utf-8')
print('Signed application targeted acceptance passed', flush=True)
