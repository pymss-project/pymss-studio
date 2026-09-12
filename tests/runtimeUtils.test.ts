import assert from 'node:assert/strict'
import test from 'node:test'

import {
  activeRuntimeEnvironment,
  runtimeToActivate,
  detectRuntimePlatform,
  isKnownRuntimeBackend,
  preferredInstalledRuntimeBackend,
  recommendedRuntimeBackend,
  runtimeAcceleratorReady,
  runtimeBackendLabel,
  runtimeCoreUpdateAvailable,
  runtimeCoreSyncAvailable,
  runtimeEnvironmentForBackend,
  runtimeManifestStatus,
  runtimeSizeHint,
} from '../src/utils/runtime.ts'

test('platform detection prefers the worker report over navigator', () => {
  // WKWebView pins navigator.platform to "MacIntel" even on Apple Silicon, so the worker's
  // platform.machine() is the only thing that can identify an arm64 Mac.
  assert.deepEqual(
    detectRuntimePlatform({ platform: 'darwin', machine: 'arm64' }),
    { isMac: true, isAppleSilicon: true },
  )
  assert.deepEqual(
    detectRuntimePlatform({ platform: 'darwin', machine: 'x86_64' }),
    { isMac: true, isAppleSilicon: false },
  )
  assert.deepEqual(
    detectRuntimePlatform({ platform: 'win32', machine: 'AMD64' }),
    { isMac: false, isAppleSilicon: false },
  )
})

test('platform detection accepts aarch64 as Apple Silicon', () => {
  assert.equal(detectRuntimePlatform({ platform: 'darwin', machine: 'aarch64' }).isAppleSilicon, true)
})

test('platform detection falls back to navigator before the worker has reported', () => {
  const original = globalThis.navigator
  Object.defineProperty(globalThis, 'navigator', { value: { platform: 'Win32' }, configurable: true })
  try {
    assert.deepEqual(detectRuntimePlatform(null), { isMac: false, isAppleSilicon: false })
    assert.deepEqual(detectRuntimePlatform({}), { isMac: false, isAppleSilicon: false })
  } finally {
    if (original === undefined) delete (globalThis as { navigator?: unknown }).navigator
    else Object.defineProperty(globalThis, 'navigator', { value: original, configurable: true })
  }
})

test('accelerator readiness judges MLX by its package, not by CUDA availability', () => {
  // The worker fills acceleratorAvailable from torch.cuda.is_available(), which is always
  // false on macOS — MLX would otherwise always render as unavailable.
  assert.equal(runtimeAcceleratorReady({ acceleratorAvailable: false, packages: { mlx: true } }, 'mlx'), true)
  assert.equal(runtimeAcceleratorReady({ acceleratorAvailable: false, packages: { mlx: false } }, 'mlx'), false)
  assert.equal(runtimeAcceleratorReady({ acceleratorAvailable: false }, 'mlx'), false)
})

test('accelerator readiness uses the reported flag for non-MLX backends', () => {
  assert.equal(runtimeAcceleratorReady({ acceleratorAvailable: true }, 'cuda'), true)
  assert.equal(runtimeAcceleratorReady({ acceleratorAvailable: false }, 'cuda'), false)
  // A backend with no installed environment has no accelerator to report on.
  assert.equal(runtimeAcceleratorReady(undefined, 'cuda'), false)
})

test('active runtime environment follows active-runtime state', () => {
  const info = {
    installedBackend: 'cuda',
    installState: { backend: 'cuda', pythonPath: 'package/python.exe' },
    installedEnvironments: [
      { backend: 'cpu', pythonPath: 'user/cpu/python.exe' },
      { backend: 'cuda', pythonPath: 'package/python.exe', torchBackend: 'cuda' },
    ],
    torchBackend: 'cuda',
  }
  assert.equal(activeRuntimeEnvironment(info)?.backend, 'cuda')
  assert.equal(preferredInstalledRuntimeBackend(info), 'cuda')
})

test('active runtime path comparison accepts the Windows long-path prefix', () => {
  const info = {
    installedBackend: 'cuda',
    installState: { backend: 'cuda', pythonPath: '\\\\?\\D:\\Pymss\\runtime-envs\\cuda\\Scripts\\python.exe' },
    installedEnvironments: [
      { backend: 'cuda', pythonPath: 'd:/pymss/runtime-envs/cuda/Scripts/python.exe' },
      { backend: 'cuda', pythonPath: 'd:/pymss/runtime-envs/other/Scripts/python.exe' },
    ],
  }
  assert.equal(activeRuntimeEnvironment(info)?.pythonPath, 'd:/pymss/runtime-envs/cuda/Scripts/python.exe')
})

test('active runtime environment does not guess when the recorded path is stale', () => {
  const info = {
    installedBackend: 'cuda',
    installState: { backend: 'cuda', pythonPath: 'missing/python.exe' },
    installedEnvironments: [
      { backend: 'cuda', pythonPath: 'first/python.exe' },
      { backend: 'cuda', pythonPath: 'second/python.exe' },
    ],
  }
  assert.equal(activeRuntimeEnvironment(info), undefined)
})

test('preferred installed backend falls back to the only installed environment', () => {
  assert.equal(preferredInstalledRuntimeBackend({
    installedEnvironments: [{ backend: 'cpu', pythonPath: 'user/cpu/python.exe' }],
  }), 'cpu')
})

test('preferred installed backend avoids guessing when several inactive environments exist', () => {
  assert.equal(preferredInstalledRuntimeBackend({
    installedEnvironments: [
      { backend: 'cpu', pythonPath: 'user/cpu/python.exe' },
      { backend: 'cuda', pythonPath: 'user/cuda/python.exe' },
    ],
  }), null)
})

test('runtime environment lookup prefers the active source when backend appears twice', () => {
  const info = {
    installedBackend: 'cuda',
    installState: { backend: 'cuda', pythonPath: 'package/python.exe' },
    installedEnvironments: [
      { backend: 'cuda', pythonPath: 'user/python.exe' },
      { backend: 'cuda', pythonPath: 'package/python.exe' },
    ],
  }
  assert.equal(runtimeEnvironmentForBackend(info, 'cuda')?.pythonPath, 'package/python.exe')
})

test('runtime environment lookup does not guess when the active path is stale', () => {
  const info = {
    installedBackend: 'cuda',
    installState: { backend: 'cuda', pythonPath: 'missing/python.exe' },
    installedEnvironments: [
      { backend: 'cuda', pythonPath: 'first/python.exe' },
      { backend: 'cuda', pythonPath: 'second/python.exe' },
    ],
  }
  assert.equal(runtimeEnvironmentForBackend(info, 'cuda'), undefined)
})

test('runtime core update is available when pymss-core alone is behind', () => {
  assert.equal(runtimeCoreUpdateAvailable({ manifestVersion: '2026.09.1', pymssVersion: '2.0.19', pymssCoreVersion: '0.1.4' }, '2.0.19', '0.1.6', '2026.09.1'), true)
})

test('runtime core update is hidden for non-updatable bootstrap runtimes', () => {
  assert.equal(runtimeCoreUpdateAvailable({ manifestVersion: '2026.09.1', pymssVersion: '2.0.18', pymssCoreVersion: '0.1.4', coreUpdateSupported: false }, '2.0.19', '0.1.6', '2026.09.1'), false)
})

test('runtime core update is hidden when the installed version is newer than PyPI', () => {
  assert.equal(runtimeCoreUpdateAvailable({ manifestVersion: '2026.09.1', pymssVersion: '2.0.20', pymssCoreVersion: '0.1.7' }, '2.0.19', '0.1.6', '2026.09.1'), false)
})

test('package updates respect manifest compatibility in addition to package versions', () => {
  const env = { pymssVersion: '2.1.3', pymssCoreVersion: '0.1.6' }
  for (const manifestVersion of ['2026.09.1', '2026.08.1']) {
    assert.equal(runtimeCoreUpdateAvailable({ ...env, manifestVersion }, '2.1.4', '0.1.6', '2026.09.1'), true)
  }
  for (const manifestVersion of ['2026.10.1', undefined, '', 'legacy', '2026.09.1-invalid']) {
    const runtime = { ...env, manifestVersion }
    assert.equal(runtimeCoreUpdateAvailable(runtime, '2.1.4', '0.1.6', '2026.09.1'), false)
    assert.equal(runtimeCoreSyncAvailable(runtime, '2026.09.1'), false)
  }
  assert.equal(runtimeCoreUpdateAvailable({ ...env, manifestVersion: '2026.09.1' }, '2.1.4', '0.1.6', undefined), false)
})

test('the only bundled runtime is selected for first-launch activation', () => {
  const bundled = { backend: 'cpu', source: 'bundled', pythonPath: 'runtime-envs/cpu/Scripts/python.exe' }
  assert.equal(runtimeToActivate({ ready: false, installedEnvironments: [bundled] }), bundled)
  assert.equal(runtimeToActivate({ ready: true, installedEnvironments: [bundled] }), undefined)
})

test('startup activation does not guess between multiple environments', () => {
  assert.equal(runtimeToActivate({
    ready: false,
    installedEnvironments: [
      { backend: 'cpu', source: 'bundled', pythonPath: 'cpu/python.exe' },
      { backend: 'cuda', source: 'bundled', pythonPath: 'cuda/python.exe' },
    ],
  }), undefined)
  assert.equal(runtimeToActivate({
    ready: false,
    installedEnvironments: [{ backend: 'cpu', source: 'managed', pythonPath: 'cpu/python.exe' }],
  })?.backend, 'cpu')
  assert.equal(runtimeToActivate({
    ready: false,
    installedEnvironments: [
      { backend: 'cpu', source: 'managed', pythonPath: 'managed/python.exe' },
      { backend: 'cuda', source: 'bundled', pythonPath: 'bundled/python.exe' },
    ],
  }), undefined)
})

test('startup activation does not replace an active managed runtime that is not ready', () => {
  assert.equal(runtimeToActivate({
    ready: false,
    installedBackend: 'cuda',
    installState: { backend: 'cuda', pythonPath: 'cuda/Scripts/python.exe' },
    installedEnvironments: [
      { backend: 'cuda', source: 'managed', pythonPath: 'cuda/Scripts/python.exe' },
      { backend: 'cpu', source: 'bundled', pythonPath: 'cpu/Scripts/python.exe', coreUpdateSupported: false },
    ],
  }), undefined)
})

test('startup activation skips a uniquely detected broken runtime', () => {
  const broken = { backend: 'cpu', pythonPath: 'cpu/python.exe', health: 'broken' }
  assert.equal(runtimeToActivate({ ready: false, installedEnvironments: [broken] }), undefined)
})

test('runtime core sync is available for an older dependency manifest', () => {
  assert.equal(runtimeCoreSyncAvailable({ manifestVersion: '2026.07.2' }, '2026.08.1'), true)
})

test('runtime core sync is not offered to a newer environment after app downgrade', () => {
  assert.equal(runtimeCoreSyncAvailable({ manifestVersion: '2026.09.1' }, '2026.08.1'), false)
})

test('runtime core sync is available when advanced workflow support is missing', () => {
  assert.equal(runtimeCoreSyncAvailable({ manifestVersion: '2026.08.1', pymssGraphAvailable: false }, '2026.08.1'), true)
})

test('runtime core sync is not offered to a legacy environment without a manifest marker', () => {
  assert.equal(runtimeCoreSyncAvailable({ pymssGraphAvailable: false }, '2026.08.1'), false)
})

test('runtime core sync stays hidden for bundled environments', () => {
  assert.equal(runtimeCoreSyncAvailable({ manifestVersion: '2026.07.2', coreUpdateSupported: false }, '2026.08.1'), false)
})

test('backend labels stay readable for unknown backends', () => {
  assert.equal(runtimeBackendLabel('mlx'), 'Apple MLX')
  assert.equal(runtimeBackendLabel('cuda'), 'NVIDIA CUDA')
  assert.equal(runtimeBackendLabel('rocm'), 'AMD ROCm')
  assert.equal(runtimeBackendLabel('cpu'), 'CPU')
  assert.equal(runtimeBackendLabel('something-else'), 'SOMETHING-ELSE')
  assert.equal(runtimeBackendLabel(null), '')
})

test('backend recognition does not leak Object.prototype keys', () => {
  assert.equal(isKnownRuntimeBackend('mlx'), true)
  assert.equal(isKnownRuntimeBackend('toString'), false)
  assert.equal(isKnownRuntimeBackend('constructor'), false)
})

test('GPU vendor decides the recommended backend', () => {
  assert.equal(recommendedRuntimeBackend({ platform: 'win32', gpuVendors: ['nvidia'] }), 'cuda')
  assert.equal(recommendedRuntimeBackend({ platform: 'win32', gpuVendors: ['amd'] }), 'rocm')
  assert.equal(recommendedRuntimeBackend({ platform: 'win32', gpuVendors: ['intel'] }), 'cpu')
})

test('a discrete NVIDIA card outranks an integrated AMD one', () => {
  assert.equal(recommendedRuntimeBackend({ platform: 'win32', gpuVendors: ['amd', 'nvidia'] }), 'cuda')
})

test('ROCm is never recommended off Windows', () => {
  // The manifest restricts rocm to win32; the installer rejects it anywhere else.
  assert.equal(recommendedRuntimeBackend({ platform: 'linux', gpuVendors: ['amd'] }), 'cpu')
  assert.equal(recommendedRuntimeBackend({ platform: 'linux', gpuVendors: ['nvidia'] }), 'cuda')
})

test('macOS is decided by architecture, not by GPU vendor', () => {
  assert.equal(recommendedRuntimeBackend({ platform: 'darwin', machine: 'arm64' }), 'mlx')
  assert.equal(recommendedRuntimeBackend({ platform: 'darwin', machine: 'x86_64' }), 'cpu')
})

test('undetectable hardware yields no recommendation at all', () => {
  // null means "no opinion" — the UI must keep offering every backend, because a missed
  // card would otherwise lock a user out of the backend they actually need.
  assert.equal(recommendedRuntimeBackend({ platform: 'win32', gpuVendors: [] }), null)
  assert.equal(recommendedRuntimeBackend({ platform: 'win32' }), null)
  assert.equal(recommendedRuntimeBackend(null), null)
})

test('manifest status compares the environment against the shipped manifest', () => {
  assert.equal(runtimeManifestStatus({ manifestVersion: '2026.07.1' }, '2026.07.1'), 'current')
  assert.equal(runtimeManifestStatus({ manifestVersion: '2026.06.2' }, '2026.07.1'), 'older')
})

test('a newer environment than the app is kept distinct from an older one', () => {
  assert.equal(runtimeManifestStatus({ manifestVersion: '2026.09.1' }, '2026.07.1'), 'newer')
})

test('manifest comparison validates the entire marker before comparing', () => {
  for (const manifestVersion of ['legacy', '2026.09.1-invalid', '2026..09.1', '2026.09.1 ']) {
    const expected = manifestVersion.trim() === '2026.09.1' ? 'current' : 'unknown'
    assert.equal(runtimeManifestStatus({ manifestVersion }, '2026.09.1'), expected)
  }
  assert.equal(runtimeManifestStatus({ manifestVersion: 'legacy' }, 'legacy'), 'unknown')
  assert.equal(runtimeManifestStatus({ manifestVersion: '2026.9.1.0' }, '2026.09.1'), 'current')
})

test('manifest status is unknown when either side did not record a version', () => {
  // The bootstrap interpreter never records one — claiming it is outdated would be a lie.
  assert.equal(runtimeManifestStatus({}, '2026.07.1'), 'unknown')
  assert.equal(runtimeManifestStatus(undefined, '2026.07.1'), 'unknown')
  assert.equal(runtimeManifestStatus({ manifestVersion: '2026.07.1' }, undefined), 'unknown')
  assert.equal(runtimeManifestStatus({ manifestVersion: '' }, ''), 'unknown')
})

test('every shipped backend has its own download size hint', () => {
  const hints = ['cpu', 'cuda', 'rocm', 'mlx'].map(runtimeSizeHint)
  assert.equal(new Set(hints).size, hints.length)
  assert.equal(runtimeSizeHint('unknown-backend'), '~1 GB')
})
