import type { InstalledRuntime, RuntimeBackend, RuntimeInfo } from '@/stores/app'

/** Approximate download size of each backend's dependency set, shown before installing. */
const RUNTIME_SIZE_HINTS: Record<RuntimeBackend, string> = {
  cpu: '~800 MB',
  cuda: '~3 GB',
  rocm: '~5 GB',
  mlx: '~600 MB',
}

export function runtimeSizeHint(backend: RuntimeBackend | string) {
  return RUNTIME_SIZE_HINTS[backend as RuntimeBackend] || '~1 GB'
}

const RUNTIME_BACKEND_LABELS: Record<RuntimeBackend, string> = {
  cpu: 'CPU',
  cuda: 'NVIDIA CUDA',
  rocm: 'AMD ROCm',
  mlx: 'Apple MLX',
}

export function isKnownRuntimeBackend(backend: string): backend is RuntimeBackend {
  return Object.prototype.hasOwnProperty.call(RUNTIME_BACKEND_LABELS, backend)
}

export function runtimeBackendLabel(backend: RuntimeBackend | string | null | undefined) {
  const key = String(backend || '')
  return isKnownRuntimeBackend(key) ? RUNTIME_BACKEND_LABELS[key] : key.toUpperCase()
}

function runtimePathKey(path: string | null | undefined) {
  let value = String(path || '').trim()
  if (!value) return ''
  if (/^\\\\\?\\UNC\\/i.test(value)) value = `\\\\${value.slice(8)}`
  else if (/^\\\\\?\\/.test(value)) value = value.slice(4)
  const windowsPath = /^[a-z]:[\\/]/i.test(value) || /^\\\\/.test(value)
  if (windowsPath) return value.replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase()
  return value.replace(/\/+$/, '')
}

export function activeRuntimeEnvironment(info: RuntimeInfo | null | undefined): InstalledRuntime | undefined {
  const environments = info?.installedEnvironments || []
  const activeBackend = String(info?.installedBackend || info?.installState?.backend || '')
  if (activeBackend) {
    const activePythonPath = String(info?.installState?.pythonPath || '')
    const candidates = environments.filter((entry) => entry.backend === activeBackend)
    if (!activePythonPath) return candidates.length === 1 ? candidates[0] : undefined
    const activePathKey = runtimePathKey(activePythonPath)
    return candidates.find((entry) => runtimePathKey(entry.pythonPath) === activePathKey)
  }

  const backend = info?.packages?.mlx ? 'mlx' : info?.torchBackend
  return backend ? environments.find((entry) => entry.backend === backend) : undefined
}

export function runtimeEnvironmentForBackend(
  info: RuntimeInfo | null | undefined,
  backend: RuntimeBackend | string,
): InstalledRuntime | undefined {
  const active = activeRuntimeEnvironment(info)
  // Prioritize the active environment if it matches the backend
  if (active?.backend === backend) return active
  // An explicitly recorded backend with no matching active path is ambiguous.
  // Do not silently select an arbitrary environment from the same backend.
  const recordedBackend = String(info?.installedBackend || info?.installState?.backend || '')
  if (recordedBackend === backend) return undefined
  // Otherwise return the first matching backend
  return (info?.installedEnvironments || []).find((entry) => entry.backend === backend)
}

export function preferredInstalledRuntimeBackend(info: RuntimeInfo | null | undefined): RuntimeBackend | null {
  const active = activeRuntimeEnvironment(info)
  const activeBackend = String(active?.backend || '')
  if (isKnownRuntimeBackend(activeBackend)) return activeBackend

  const installed = (info?.installedEnvironments || [])
    .map((entry) => String(entry.backend || ''))
    .filter(isKnownRuntimeBackend)

  // Multiple environments are safe to select only when they all use the same backend.
  const uniqueBackends = [...new Set(installed)]
  if (uniqueBackends.length === 1) return uniqueBackends[0]

  return null
}

/**
 * Return the only ready runtime that should be made active when the pointer is absent.
 *
 * This covers both a fresh packaged install and an overwrite where the existing active
 * pointer was lost.  Selecting is safe only when exactly one usable environment remains;
 * multiple environments require an explicit user choice.
 */
export function runtimeToActivate(info: RuntimeInfo | null | undefined): InstalledRuntime | undefined {
  if (info?.ready) return undefined
  // Never replace an environment that is already recorded as active. A managed runtime
  // may be temporarily unready (for example after a failed package update).
  const activeBackend = String(info?.installedBackend || info?.installState?.backend || '').trim()
  if (activeBackend || info?.installState?.pythonPath) return undefined
  const candidates = (info?.installedEnvironments || []).filter((entry) =>
    isKnownRuntimeBackend(String(entry.backend || ''))
      && Boolean(entry.pythonPath)
      && entry.health !== 'broken',
  )
  return candidates.length === 1 ? candidates[0] : undefined
}

export function runtimeCoreUpdateAvailable(
  env: InstalledRuntime | undefined,
  latestPymssVersion: string | null | undefined,
  latestPymssCoreVersion: string | null | undefined,
  currentManifestVersion: string | undefined,
) {
  if (!env || env.coreUpdateSupported === false) return false
  const manifestStatus = runtimeManifestStatus(env, currentManifestVersion)
  if (manifestStatus !== 'current' && manifestStatus !== 'older') return false
  const pymssVersion = env.pymssVersion || env.packageVersions?.pymss || ''
  const pymssCoreVersion = env.pymssCoreVersion || env.packageVersions?.['pymss-core'] || ''
  return Boolean(
    versionGreaterThan(latestPymssVersion, pymssVersion)
    || versionGreaterThan(latestPymssCoreVersion, pymssCoreVersion),
  )
}

/**
 * Whether an installed, user-managed environment needs a core synchronisation even when the
 * package versions reported by PyPI are already current. This covers dependency-manifest changes
 * and the graph API used by advanced workflows.
 */
export function runtimeCoreSyncAvailable(
  env: InstalledRuntime | undefined,
  currentManifestVersion: string | undefined,
) {
  if (!env || env.coreUpdateSupported === false) return false
  const manifestStatus = runtimeManifestStatus(env, currentManifestVersion)
  return manifestStatus === 'older'
    || (manifestStatus === 'current' && env.pymssGraphAvailable === false)
}

function versionGreaterThan(candidate: string | null | undefined, current: string | null | undefined) {
  const left = parseVersionParts(candidate)
  const right = parseVersionParts(current)
  if (!left || !right) return false
  const length = Math.max(left.length, right.length)
  for (let index = 0; index < length; index += 1) {
    const difference = (left[index] || 0) - (right[index] || 0)
    if (difference !== 0) return difference > 0
  }
  return false
}

function parseVersionParts(value: string | null | undefined) {
  const core = String(value || '').trim().match(/^\d+(?:\.\d+)*/)?.[0]
  if (!core) return null
  return core.split('.').map((part) => Number(part))
}

/**
 * Whether the environment's accelerator is usable.
 * The worker reports `acceleratorAvailable` from `torch.cuda.is_available()`, which is always
 * false on macOS, so MLX has to be judged by its package instead (matching the worker's own
 * readiness check in worker_bootstrap.py).
 */
export function runtimeAcceleratorReady(env: InstalledRuntime | undefined, backend: RuntimeBackend | string) {
  if (!env) return false
  if (backend === 'mlx') return Boolean(env.packages?.mlx)
  return Boolean(env.acceleratorAvailable)
}

/**
 * Which backends this machine can run.
 * Prefers what the worker reports (`sys.platform` / `platform.machine()`): WKWebView pins
 * `navigator.platform` to "MacIntel" even on Apple Silicon, so the browser can never identify
 * an arm64 Mac and MLX would stay invisible. navigator is only a pre-detection fallback.
 */
export function detectRuntimePlatform(info: RuntimeInfo | null | undefined) {
  const reported = String(info?.platform || '')
  if (reported) {
    const machine = String(info?.machine || '').toLowerCase()
    const isMac = reported === 'darwin'
    return { isMac, isAppleSilicon: isMac && (machine.includes('arm') || machine.includes('aarch64')) }
  }
  const isMac = /Mac/i.test(navigator.platform)
  return { isMac, isAppleSilicon: isMac && /arm/i.test(navigator.platform) }
}

/**
 * The backend this machine should be running, from GPU vendor plus platform.
 *
 * Returns null when nothing can be recommended — callers must treat that as "no opinion" and
 * still offer every backend. Detection can miss a card, and hiding options on a false negative
 * would leave a user unable to install the backend they actually need.
 */
export function recommendedRuntimeBackend(info: RuntimeInfo | null | undefined): RuntimeBackend | null {
  const { isMac, isAppleSilicon } = detectRuntimePlatform(info)
  if (isMac) return isAppleSilicon ? 'mlx' : 'cpu'
  const vendors = info?.gpuVendors
  if (!vendors?.length) return null
  // A discrete NVIDIA card wins over an AMD integrated one when both are present.
  if (vendors.includes('nvidia')) return 'cuda'
  // ROCm is Windows-only in the manifest; recommending it elsewhere would fail at install time.
  if (vendors.includes('amd')) return String(info?.platform || '') === 'win32' ? 'rocm' : 'cpu'
  return 'cpu'
}

export type RuntimeManifestStatus = 'current' | 'older' | 'newer' | 'unknown'

/**
 * Whether an environment was built from the dependency manifest the app ships today.
 *
 * Keeps older and newer environments distinct. An older environment can be synchronized in
 * place; a newer environment usually means the app was downgraded and must not be rewritten with
 * the older app's manifest marker.
 */
export function runtimeManifestStatus(
  env: InstalledRuntime | undefined,
  currentManifestVersion: string | undefined,
): RuntimeManifestStatus {
  const expected = String(currentManifestVersion || '').trim()
  const actual = String(env?.manifestVersion || '').trim()
  const markerPattern = /^[0-9]+(?:\.[0-9]+)*$/
  if (!markerPattern.test(expected) || !markerPattern.test(actual)) return 'unknown'
  if (actual === expected) return 'current'
  const expectedParts = parseVersionParts(expected)
  const actualParts = parseVersionParts(actual)
  if (!expectedParts || !actualParts) return 'unknown'
  const length = Math.max(expectedParts.length, actualParts.length)
  for (let index = 0; index < length; index += 1) {
    const difference = (actualParts[index] || 0) - (expectedParts[index] || 0)
    if (difference !== 0) return difference < 0 ? 'older' : 'newer'
  }
  return 'current'
}
