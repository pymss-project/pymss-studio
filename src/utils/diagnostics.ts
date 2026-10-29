import type { EnvInfo, RuntimeInfo, WorkerEventConnectionStatus } from '../stores/app.ts'
import type { OptionalPackageStatus } from '../features/runtime-packages/optionalRuntimePackage.ts'

export type DiagnosticState = 'ok' | 'warn' | 'error' | 'unknown' | 'optional'
export type DiagnosticCheck = {
  key: string
  group: 'runtime' | 'capabilities' | 'connection'
  label: string
  state: DiagnosticState
  value?: string
  detail?: string
  hint?: string
  action: 'runtime' | 'tools' | 'events'
}

const stateFor = (available: boolean | null | undefined, missing: DiagnosticState = 'error'): DiagnosticState =>
  available === true ? 'ok' : available === false ? missing : 'unknown'

export function isDiagnosticIssueEvent(event: { type?: string; payload?: unknown }): boolean {
  if (/error|failed|warning/i.test(String(event.type))) return true
  if (!event.payload || typeof event.payload !== 'object') return false
  const payload = event.payload as Record<string, unknown>
  if (event.type === 'workflow_execution') {
    return /(?:^|_)failed$/i.test(String(payload.event || ''))
  }
  const record = payload.logRecord && typeof payload.logRecord === 'object' ? payload.logRecord as Record<string, unknown> : payload
  return ['task_log', 'worker_log', 'download_log', 'optional_package_log', 'runtime_install_log', 'runtime_core_update_log'].includes(String(event.type))
    && ['warn', 'warning', 'error', 'fatal', 'critical'].includes(String(record.level || '').toLowerCase())
}

export function buildDiagnosticChecks(input: {
  env: EnvInfo | null
  runtime: RuntimeInfo | null
  envError?: string | null
  runtimeError?: string
  connection: WorkerEventConnectionStatus
  connectionError?: string
  optionalPackage?: OptionalPackageStatus | null
  optionalError?: string
  isMac: boolean
}): DiagnosticCheck[] {
  const { env, runtime } = input
  const packages = runtime?.packages
  const checks: DiagnosticCheck[] = [
    { key: 'python', group: 'runtime', label: 'Python', state: env?.pythonVersion ? 'ok' : 'unknown', value: env?.pythonVersion, detail: env?.platform, action: 'runtime' },
    { key: 'pymss', group: 'runtime', label: 'pymss', state: stateFor(env?.pymssAvailable ?? packages?.pymss), value: env?.pymssVersion || runtime?.pymssVersion || undefined, detail: env?.pymssError, hint: 'debug.hintCore', action: 'runtime' },
    { key: 'torch', group: 'runtime', label: 'PyTorch', state: stateFor(env?.torchAvailable ?? packages?.torch), value: env?.torchVersion || runtime?.torchVersion || undefined, detail: env?.torchError, hint: 'debug.hintCore', action: 'runtime' },
    { key: 'graph', group: 'capabilities', label: 'debug.checkGraph', state: stateFor(runtime?.pymssGraphAvailable, 'warn'), hint: 'debug.hintGraph', action: 'runtime' },
    { key: 'customModels', group: 'capabilities', label: 'debug.checkCustomModels', state: stateFor(env?.customModelsSupported, 'warn'), hint: 'debug.hintCustomModels', action: 'runtime' },
    { key: 'av', group: 'capabilities', label: 'PyAV', state: stateFor(env?.avAvailable ?? packages?.av, 'warn'), hint: 'debug.hintAv', action: 'runtime' },
    { key: 'librosa', group: 'capabilities', label: 'librosa', state: stateFor(env?.librosaAvailable ?? packages?.librosa, 'warn'), hint: 'debug.hintLibrosa', action: 'runtime' },
    { key: 'audioread', group: 'capabilities', label: 'audioread', state: stateFor(packages?.audioread, 'warn'), hint: 'debug.hintAudioread', action: 'runtime' },
  ]

  const probeError = input.runtimeError || runtime?.liveProbeError?.message || input.envError
  checks.unshift({
    key: 'runtime', group: 'runtime', label: 'debug.checkRuntime',
    state: probeError ? 'error' : stateFor(runtime?.ready),
    value: runtime?.installedBackend || runtime?.torchBackend || undefined,
    detail: probeError || undefined, hint: 'debug.hintRuntime', action: 'runtime',
  })

  const acceleratorError = env?.cudaAvailableError || env?.cudaDeviceCountError || env?.cudaDeviceNamesError
  const backend = env?.torchBackend || runtime?.torchBackend
  const hasDeviceProbe = [env?.cudaAvailable, env?.mpsAvailable, env?.mlxAvailable].some(value => typeof value === 'boolean')
  const accelerated = hasDeviceProbe ? env?.cudaAvailable || env?.mpsAvailable || env?.mlxAvailable : runtime?.acceleratorAvailable
  const acceleratorKnown = hasDeviceProbe || typeof runtime?.acceleratorAvailable === 'boolean' || backend === 'cpu'
  checks.push({
    key: 'accelerator', group: 'runtime', label: 'debug.checkAccelerator',
    state: acceleratorError ? 'warn' : accelerated ? 'ok' : !acceleratorKnown ? 'unknown' : backend === 'cpu' ? 'ok' : 'warn',
    value: accelerated ? env?.mlxAvailable ? 'MLX' : env?.mpsAvailable ? 'MPS' : String(backend || 'CUDA').toUpperCase() : acceleratorKnown ? 'CPU' : undefined,
    detail: acceleratorError || undefined, hint: 'debug.hintAccelerator', action: 'runtime',
  })

  if (!input.isMac) {
    const optional = input.optionalPackage
    checks.push({
      key: 'funasr', group: 'capabilities', label: 'FunASR',
      state: input.optionalError ? 'unknown' : optional?.installed ? 'ok' : optional?.present || optional?.issue ? 'warn' : optional ? 'optional' : 'unknown',
      value: optional?.version || undefined, detail: input.optionalError || undefined,
      hint: 'debug.hintFunasr', action: 'tools',
    })
  }

  checks.push({
    key: 'connection', group: 'connection', label: 'debug.checkConnection',
    state: input.connection === 'connected' ? 'ok' : input.connection === 'error' ? 'error' : 'unknown',
    detail: input.connectionError || undefined, hint: 'debug.hintConnection', action: 'events',
  })
  return checks
}

export function summarizeDiagnosticChecks(checks: readonly DiagnosticCheck[]) {
  const counts = { ok: 0, warn: 0, error: 0, unknown: 0, optional: 0 }
  for (const check of checks) counts[check.state] += 1
  const state: DiagnosticState = counts.error ? 'error' : counts.warn ? 'warn' : counts.unknown || !checks.length ? 'unknown' : 'ok'
  return { ...counts, state }
}
