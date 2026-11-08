/** Keep worker diagnostics intact while translating the user-facing summary by code. */
export class RuntimeOperationError extends Error {
  readonly code: string
  readonly diagnostics: Record<string, unknown>

  constructor(value: unknown) {
    const source = value !== null && typeof value === 'object' ? value as Record<string, unknown> : {}
    super(typeof source.message === 'string' ? source.message : String(value ?? 'Runtime operation failed'))
    this.name = 'RuntimeOperationError'
    this.code = typeof source.code === 'string' ? source.code : ''
    this.diagnostics = Object.fromEntries(Object.entries(source).filter(([key]) => !['name', 'code', 'message', 'stack'].includes(key)))
  }
}

export function runtimeOperationError(value: unknown): RuntimeOperationError {
  return value instanceof RuntimeOperationError ? value : new RuntimeOperationError(value)
}

const SUMMARY_KEYS: Record<string, string> = {
  RUNTIME_MANIFEST_INCOMPATIBLE: 'settings.runtimeCoreManifestIncompatible',
  RUNTIME_CORE_UPDATE_INACTIVE: 'settings.runtimeCoreInactive',
  RUNTIME_NOT_INSTALLED: 'settings.runtimeCoreNotInstalled',
  RUNTIME_UPDATE_PLAN_INVALID: 'settings.runtimeCorePlanInvalid',
  RUNTIME_UPDATE_PLAN_STALE: 'settings.runtimeCorePlanInvalid',
  RUNTIME_BUSY: 'settings.runtimeCoreBusy',
  RUNTIME_PERMISSION_DENIED: 'settings.runtimeCorePermissionDenied',
  TASK_CANCELLED: 'settings.runtimeCoreOperationCancelled',
  RUNTIME_PROFILE_INVALID: 'settings.runtimeProfileInvalid',
  RUNTIME_PROFILE_STATE_CHANGED: 'settings.runtimeCorePlanInvalid',
  RUNTIME_PROFILE_MISMATCH: 'settings.runtimeCorePlanInvalid',
  RUNTIME_HARDWARE_INVENTORY_FAILED: 'settings.runtimeHardwareInventoryFailed',
  RUNTIME_PROFILE_DISABLED: 'settings.runtimeProfileUnavailable',
  RUNTIME_PREVIEW_ACCEPTANCE_REQUIRED: 'settings.runtimeRocmAcceptPreview',
  RUNTIME_PREVIOUS_EXISTS: 'settings.runtimePreviousExists',
  RUNTIME_RECOVERY_REQUIRED: 'settings.runtimeRecoveryRequired',
  RUNTIME_RECOVERY_FAILED: 'settings.runtimeRecoveryFailed',
  RUNTIME_ROLLBACK_FAILED: 'settings.runtimeEnvironmentRollbackFailed',
  RUNTIME_GPU_PROBE_FAILED: 'settings.runtimeGpuProbeFailed',
  RUNTIME_GPU_VALIDATION_FAILED: 'settings.runtimeRocmActivationFailed',
  RUNTIME_GPU_PROBE_TIMEOUT: 'settings.runtimeGpuProbeTimeout',
  ROCM_UNAVAILABLE: 'settings.runtimeGpuUnavailable',
  ROCM_RUNTIME_UNAVAILABLE: 'settings.runtimeGpuUnavailable',
  ROCM_DEVICE_UNAVAILABLE: 'settings.runtimeGpuUnavailable',
  ROCM_PROBE_INVALID_REQUEST: 'settings.runtimeGpuProbeInvalid',
  ROCM_PROBE_CLEANUP_FAILED: 'settings.runtimeGpuProbeCleanupFailed',
  ROCM_PROBE_FAILED: 'settings.runtimeGpuProbeFailed',
  ROCM_PROBE_TIMEOUT: 'settings.runtimeGpuProbeTimeout',
  ROCM_CONTEXT_LOST: 'settings.runtimeRocmContextLost',
  ROCM_OUT_OF_MEMORY: 'settings.runtimeRocmOutOfMemory',
  ROCM_TARGET_MISSING: 'settings.runtimeRocmTargetMissing',
  ROCM_RUNTIME_INCOMPATIBLE: 'settings.runtimeRocmIncompatible',
  ROCM_NATIVE_CHECK_FAILED: 'settings.runtimeRocmNativeCheckFailed',
}

export function runtimeErrorSummary(value: unknown, translate: (key: string) => string): string {
  const code = runtimeOperationError(value).code
  return translate(Object.hasOwn(SUMMARY_KEYS, code) ? SUMMARY_KEYS[code]! : 'settings.runtimeCoreOperationFailed')
}

export function runtimeErrorDetails(value: unknown): string {
  const error = runtimeOperationError(value)
  const details = Object.entries(error.diagnostics).map(([key, value]) => {
    if (value === null || value === undefined || value === '') return ''
    if (typeof value === 'string') return `${key}: ${value}`
    try { return `${key}: ${JSON.stringify(value, null, 2)}` } catch { return `${key}: ${String(value)}` }
  })
  return [error.code, error.message, ...details].filter(Boolean).join('\n')
}
