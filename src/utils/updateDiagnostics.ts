import type { UpdateStatus } from '../stores/update'

export type UpdateDiagnosticInput = {
  status: UpdateStatus
  currentVersion: string
  latestVersion: string
  selectedSource?: 'github' | 'modelscope' | 'custom'
  lastCheckedAt: string
  error: string
  downloadDownloadedBytes: number
  downloadTotalBytes: number
  downloadSpeedBytesPerSecond: number
  downloadProgressPercent: number | null
  restartRequired: boolean
  installFailed: boolean
  integrity: { supported: boolean; valid: boolean | null; repairSupported: boolean; detail?: string | null } | null
  integrityError: string
  integrityChecking: boolean
  repairing: boolean
  downloadLogs: Array<{ at: string; message: string }>
}

export function buildUpdateDiagnosticSnapshot(input: UpdateDiagnosticInput) {
  return {
    status: input.status,
    currentVersion: input.currentVersion,
    latestVersion: input.latestVersion,
    selectedSource: input.selectedSource || null,
    lastCheckedAt: input.lastCheckedAt,
    error: input.error,
    downloadDownloadedBytes: input.downloadDownloadedBytes,
    downloadTotalBytes: input.downloadTotalBytes,
    downloadSpeedBytesPerSecond: input.downloadSpeedBytesPerSecond,
    downloadProgressPercent: input.downloadProgressPercent,
    restartRequired: input.restartRequired,
    installFailed: input.installFailed,
    integrity: input.integrity ? {
      supported: input.integrity.supported,
      valid: input.integrity.valid,
      repairSupported: input.integrity.repairSupported,
      detail: input.integrity.detail || null,
    } : null,
    integrityError: input.integrityError,
    integrityChecking: input.integrityChecking,
    repairing: input.repairing,
    downloadLogs: input.downloadLogs.slice(-20).map(entry => ({ at: entry.at, message: entry.message })),
  }
}

export type UpdateDiagnosticSnapshot = ReturnType<typeof buildUpdateDiagnosticSnapshot>
