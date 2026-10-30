import { createLogger } from '@/utils/logger'
import { defineStore } from 'pinia'
import { computed, onScopeDispose, ref, watch } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { exit } from '@tauri-apps/plugin-process'
import { getCurrentWindow } from '@tauri-apps/api/window'
import { useAppStore } from '@/stores/app'
import { useSettingsStore } from '@/stores/settings'
import { isTauriRuntime, loadAppStore, saveAppStore } from '@/utils/appStore'
import { isMainTaskOwner } from '@/utils/taskOwner'
import { registerWindowCloseGuard } from '@/utils/windowCloseGuards'
import i18n from '@/i18n'

const studioLog = createLogger('ui.stores.update')

export type UpdateStatus = 'idle' | 'checking' | 'available' | 'downloading' | 'ready' | 'installing' | 'restart-required' | 'failed'

type UpdateStorePayload = {
  deferredVersion?: string
  deferredAt?: string
  lastAcceptedVersion?: string
}

type ManagedUpdate = {
  currentVersion: string
  version: string
  date?: string
  body?: string
  prerelease: boolean
  distribution: 'inno' | 'portable' | 'macosApp' | 'appImage'
  autoUpdateSupported: boolean
  requiresManualInstall: boolean
  updateMessage?: string
  manualInstallUrl?: string
  source?: 'github' | 'modelscope' | 'custom'
}

type ApplicationIntegrity = {
  supported: boolean
  valid: boolean | null
  repairSupported: boolean
  detail?: string | null
}

type UpdateDownloadEvent =
  | { event: 'SourceSelected'; data: { source: 'github' | 'modelscope' | 'custom'; fallback?: boolean } }
  | { event: 'Started'; data: { contentLength?: number | null } }
  | { event: 'Progress'; data: { chunkLength?: number; downloadedBytes?: number; contentLength?: number | null; speedBytesPerSecond?: number } }
  | { event: 'Finished'; data?: null }
  | { event: 'RestartRequired'; data?: null }

export type UpdateLogEntry = {
  at: string
  message: string
}

function isPrereleaseVersion(version: string) {
  return /^\d+\.\d+\.\d+-/.test(version.trim())
}

export const useUpdateStore = defineStore('update', () => {
  const status = ref<UpdateStatus>('idle')
  const currentVersion = ref('')
  const latestVersion = ref('')
  const releaseNotes = ref('')
  const releaseDate = ref('')
  const updateMessage = ref('')
  const manualInstallUrl = ref('')
  const selectedSource = ref<ManagedUpdate['source']>()
  const error = ref('')
  const lastCheckedAt = ref('')
  const availableUpdate = ref<ManagedUpdate | null>(null)
  const deferredVersion = ref('')
  const deferredAt = ref('')
  const lastAcceptedVersion = ref('')
  const downloadDownloadedBytes = ref(0)
  const downloadTotalBytes = ref(0)
  const downloadSpeedBytesPerSecond = ref(0)
  const downloadLogs = ref<UpdateLogEntry[]>([])
  const installErrorVisible = ref(false)
  const installFailed = ref(false)
  const restartRequired = ref(false)
  const initialized = ref(false)
  const autoCheckCompleted = ref(false)
  const integrity = ref<ApplicationIntegrity | null>(null)
  const integrityChecking = ref(false)
  const integrityError = ref('')
  const repairing = ref(false)
  let integrityInFlight: Promise<ApplicationIntegrity | null> | null = null
  let updateCheckInFlight: Promise<ManagedUpdate | null> | null = null
  let initializeInFlight: Promise<void> | null = null
  let unlistenProgress: UnlistenFn | undefined
  let lastProgressLogAt = 0
  let lastProgressLogPercent = -1
  let installInFlight = false
  let exitInFlight: Promise<boolean> | null = null

  const hasUpdate = computed(() => availableUpdate.value !== null)
  const isBusy = computed(() => integrityChecking.value || restartRequired.value || ['checking', 'downloading', 'installing'].includes(status.value))
  const isInstallingUpdate = computed(() => restartRequired.value || ['downloading', 'installing'].includes(status.value))
  const isInstallOverlayVisible = computed(() => isInstallingUpdate.value || installErrorVisible.value)
  const installationBlockedReason = computed(() => {
    const app = useAppStore()
    const settings = useSettingsStore()
    return app.runtimeInstallStatus === 'installing'
      || app.runtimeCoreUpdateStatus === 'preparing' || app.runtimeCoreUpdateStatus === 'updating'
      || settings.isModelDirMigrating
      ? String(i18n.global.t('settings.updateTasksBusy')) : ''
  })
  if (isMainTaskOwner()) {
    const removeCloseGuard = registerWindowCloseGuard(() => {
      if (isInstallingUpdate.value && status.value !== 'restart-required') throw new Error('An application update is in progress. Wait for it to finish before closing.')
    }, 100)
    onScopeDispose(removeCloseGuard)
  }
  const downloadProgressPercent = computed(() => {
    if (downloadTotalBytes.value <= 0) return 0
    return Math.min(100, Math.round((downloadDownloadedBytes.value / downloadTotalBytes.value) * 100))
  })
  const downloadEtaSeconds = computed(() => {
    if (downloadTotalBytes.value <= 0 || downloadSpeedBytesPerSecond.value <= 0) return null
    const remaining = Math.max(0, downloadTotalBytes.value - downloadDownloadedBytes.value)
    return Math.ceil(remaining / downloadSpeedBytesPerSecond.value)
  })
  const updateIsPrerelease = computed(() => {
    return availableUpdate.value?.prerelease === true || isPrereleaseVersion(availableUpdate.value?.version || latestVersion.value)
  })
  const requiresManualInstall = computed(() => Boolean(
    availableUpdate.value
    && (availableUpdate.value.autoUpdateSupported === false || availableUpdate.value.requiresManualInstall === true),
  ))
  const shouldShowDeferred = computed(() => Boolean(
    availableUpdate.value
    && deferredVersion.value
    && deferredVersion.value === availableUpdate.value.version
    && deferredVersion.value !== lastAcceptedVersion.value,
  ))

  async function persistState() {
    if (!isMainTaskOwner()) return
    const payload: UpdateStorePayload = {
      deferredVersion: deferredVersion.value || undefined,
      deferredAt: deferredAt.value || undefined,
      lastAcceptedVersion: lastAcceptedVersion.value || undefined,
    }
    await saveAppStore('update-state', payload)
  }

  async function initialize() {
    if (initialized.value || !isMainTaskOwner()) return
    if (!initializeInFlight) {
      initializeInFlight = (async () => {
        autoCheckCompleted.value = false
        let payload: UpdateStorePayload | null = null
        try {
          payload = await loadAppStore<UpdateStorePayload>('update-state')
        } catch (error) {
          // Update state is optional; keep the progress listener alive even
          // when a damaged or older store backend cannot read it.
          studioLog.warn('Failed to load update state', error)
          payload = null
        }
        deferredVersion.value = String(payload?.deferredVersion || '')
        deferredAt.value = String(payload?.deferredAt || '')
        lastAcceptedVersion.value = String(payload?.lastAcceptedVersion || '')
        if (isTauriRuntime()) {
          const nextUnlisten = await listen<UpdateDownloadEvent>('pymss://managed-update-event', (event) => {
            if (event.payload.event === 'RestartRequired') {
              restartRequired.value = true
              // Progress events survive a renderer reload; only a local request
              // can still own persistence and the automatic exit sequence.
              if (!installInFlight && !exitInFlight) {
                status.value = 'restart-required'
                error.value = String(i18n.global.t('settings.updateRestartRequiredHint'))
              }
              return
            }
            if (restartRequired.value) return
            if (event.payload.event === 'SourceSelected') {
              selectedSource.value = event.payload.data.source
              downloadDownloadedBytes.value = 0
              downloadTotalBytes.value = 0
              downloadSpeedBytesPerSecond.value = 0
              lastProgressLogAt = 0
              lastProgressLogPercent = -1
              status.value = 'downloading'
              const source = event.payload.data.source === 'github' ? 'GitHub'
                : event.payload.data.source === 'modelscope' ? 'ModelScope' : 'Custom endpoint'
              appendDownloadLog(`${event.payload.data.fallback ? 'Retrying download from' : 'Download source:'} ${source}`)
              return
            }
            if (event.payload.event === 'Started') {
              const total = Number(event.payload.data.contentLength || 0)
              downloadTotalBytes.value = Number.isFinite(total) && total > 0 ? total : 0
              downloadDownloadedBytes.value = 0
              downloadSpeedBytesPerSecond.value = 0
              appendDownloadLog(downloadTotalBytes.value > 0
                ? `Download started (${(downloadTotalBytes.value / 1024 / 1024).toFixed(1)} MB)`
                : 'Download started (size unavailable)')
              return
            }
            if (event.payload.event === 'Progress') {
              const data = event.payload.data
              const chunk = Number(data.chunkLength || 0)
              const reportedDownloaded = Number(data.downloadedBytes)
              if (Number.isFinite(reportedDownloaded) && reportedDownloaded >= 0) {
                downloadDownloadedBytes.value = Math.max(downloadDownloadedBytes.value, reportedDownloaded)
              } else if (Number.isFinite(chunk) && chunk > 0) {
                downloadDownloadedBytes.value += chunk
              }
              const reportedTotal = Number(data.contentLength || 0)
              if (Number.isFinite(reportedTotal) && reportedTotal > 0) downloadTotalBytes.value = reportedTotal
              const speed = Number(data.speedBytesPerSecond || 0)
              if (Number.isFinite(speed) && speed >= 0) downloadSpeedBytesPerSecond.value = speed
              appendProgressLog(downloadDownloadedBytes.value, downloadTotalBytes.value, downloadSpeedBytesPerSecond.value)
              return
            }
            if (event.payload.event === 'Finished') {
              if (downloadTotalBytes.value > 0) downloadDownloadedBytes.value = downloadTotalBytes.value
              downloadSpeedBytesPerSecond.value = 0
              appendDownloadLog('Download finished; preparing installation')
              status.value = 'installing'
            }
          })
          unlistenProgress?.()
          unlistenProgress = nextUnlisten
          try {
            const pending = await invoke<{ restartRequired: boolean }>('get_managed_update_state')
            if (pending?.restartRequired) {
              restartRequired.value = true
              status.value = 'restart-required'
              error.value = String(i18n.global.t('settings.updateRestartRequiredHint'))
            }
          } catch (stateError) {
            studioLog.warn('Failed to read application update state', stateError)
          }
        }
        initialized.value = true
      })().finally(() => {
        initializeInFlight = null
      })
    }
    return initializeInFlight
  }

  function hasPendingDeferredVersion(version: string) {
    return Boolean(version && deferredVersion.value === version && version !== lastAcceptedVersion.value)
  }

  function resetResult() {
    availableUpdate.value = null
    latestVersion.value = ''
    releaseNotes.value = ''
    releaseDate.value = ''
    updateMessage.value = ''
    manualInstallUrl.value = ''
    selectedSource.value = undefined
  }

  function resetDownloadProgress() {
    downloadDownloadedBytes.value = 0
    downloadTotalBytes.value = 0
    downloadSpeedBytesPerSecond.value = 0
    downloadLogs.value = []
    lastProgressLogAt = 0
    lastProgressLogPercent = -1
  }

  function appendDownloadLog(message: string) {
    const normalized = message.trim()
    if (!normalized) return
    const at = new Date().toISOString().slice(11, 19)
    downloadLogs.value = [...downloadLogs.value.slice(-39), { at, message: normalized }]
  }

  function appendProgressLog(downloaded: number, total: number, speed: number) {
    const percent = total > 0 ? Math.min(100, Math.round((downloaded / total) * 100)) : -1
    const now = Date.now()
    if (percent >= 0 && percent !== 100 && percent - lastProgressLogPercent < 10 && now - lastProgressLogAt < 1500) return
    if (percent < 0 && now - lastProgressLogAt < 1500) return
    lastProgressLogAt = now
    lastProgressLogPercent = percent
    const progress = percent >= 0 ? ` ${percent}%` : ''
    const speedLabel = speed > 0 ? ` ${(speed / 1024 / 1024).toFixed(1)} MB/s` : ''
    appendDownloadLog(`Download progress${progress}${speedLabel}`)
  }

  async function checkForUpdates(manual = false) {
    if (!isMainTaskOwner()) return null
    if (installInFlight || isInstallingUpdate.value) return availableUpdate.value
    if (restartRequired.value) return availableUpdate.value
    const app = useAppStore()
    const settings = useSettingsStore()
    if (!manual && autoCheckCompleted.value) return availableUpdate.value
    if (!updateCheckInFlight) {
      updateCheckInFlight = (async () => {
        try {
          repairing.value = false
          installErrorVisible.value = false
          if (!app.buildInfoUpdateSupported) {
            resetResult()
            status.value = 'idle'
            error.value = ''
            currentVersion.value = app.buildInfoVersion || ''
            return null
          }
          status.value = 'checking'
          const platform = String(app.runtimeInfo?.platform || app.envInfo?.platform || (typeof navigator !== 'undefined' ? navigator.platform : ''))
          if (/mac|darwin/i.test(platform)) void checkIntegrity()
          error.value = ''
          installFailed.value = false
          const endpointOverride = settings.developerMode ? settings.updateEndpointOverride.trim() : ''
          const update = await invoke<ManagedUpdate | null>('check_managed_update', {
            channel: settings.updateChannel,
            endpointOverride: endpointOverride || null,
            source: settings.updateSource || 'auto',
          })
          if (restartRequired.value) return availableUpdate.value
          currentVersion.value = update?.currentVersion || app.buildInfoVersion || ''
          lastCheckedAt.value = new Date().toISOString()
          if (!update) {
            resetResult()
            status.value = 'idle'
            return null
          }
          availableUpdate.value = update
          latestVersion.value = update.version
          releaseNotes.value = update.body || ''
          releaseDate.value = update.date || ''
          updateMessage.value = update.updateMessage || ''
          manualInstallUrl.value = update.manualInstallUrl || ''
          selectedSource.value = update.source
          status.value = deferredVersion.value === update.version ? 'ready' : 'available'
          return update
        } catch (err) {
          if (restartRequired.value) return availableUpdate.value
          error.value = err instanceof Error ? err.message : String(err)
          status.value = 'failed'
          throw err
        } finally {
          updateCheckInFlight = null
        }
      })()
    }
    if (manual) return updateCheckInFlight
    return updateCheckInFlight.catch(() => null).finally(() => {
      autoCheckCompleted.value = true
    })
  }

  function requestUpdateExit(closeWindow: boolean): Promise<boolean> {
    if (!restartRequired.value) return Promise.resolve(false)
    if (exitInFlight) return exitInFlight
    status.value = 'installing'
    error.value = ''
    exitInFlight = (async () => {
      const bounded = async (request: Promise<unknown>) => {
        let timer: ReturnType<typeof setTimeout> | undefined
        try {
          await Promise.race([request, new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error('Application exit request timed out')), 3000)
          })])
        } finally {
          if (timer) clearTimeout(timer)
        }
      }
      try {
        if (closeWindow) {
          await bounded(invoke('close_current_window'))
        } else {
          try {
            await bounded(exit(0))
          } catch (exitError) {
            appendDownloadLog(`Application exit failed; closing the main window: ${exitError}`)
            await bounded(getCurrentWindow().destroy())
          }
        }
        return true
      } catch (exitError) {
        appendDownloadLog(`Application exit requires manual intervention: ${exitError}`)
        error.value = String(i18n.global.t('settings.updateRestartRequiredHint'))
        status.value = 'restart-required'
        return false
      } finally {
        exitInFlight = null
      }
    })()
    return exitInFlight
  }

  function retryUpdateExit() {
    return requestUpdateExit(false)
  }

  function closeForUpdate() {
    return requestUpdateExit(true)
  }

  async function checkIntegrity() {
    if (!isMainTaskOwner() || !isTauriRuntime() || isInstallingUpdate.value) return integrity.value
    if (!integrityInFlight) {
      integrityChecking.value = true
      integrityError.value = ''
      integrityInFlight = invoke<ApplicationIntegrity>('check_application_integrity')
        .then(report => { integrity.value = report; return report })
        .catch(err => {
          integrityError.value = err instanceof Error ? err.message : String(err)
          return null
        })
        .finally(() => { integrityChecking.value = false; integrityInFlight = null })
    }
    return integrityInFlight
  }

  function downloadAndInstall() {
    return installApplication(false)
  }

  function repairApplication() {
    return installApplication(true)
  }

  async function installApplication(repair: boolean) {
    if (!isMainTaskOwner()) throw new Error('Application installation is available only in the main window.')
    if (restartRequired.value) return retryUpdateExit()
    if (installInFlight || ['checking', 'downloading', 'installing'].includes(status.value) || integrityChecking.value) throw new Error('An application operation is already in progress.')
    if (installationBlockedReason.value) throw new Error(installationBlockedReason.value)
    const update = availableUpdate.value
    if (repair && (!useAppStore().buildInfoUpdateSupported || !integrity.value?.repairSupported)) {
      throw new Error('Application repair is unavailable for this installation.')
    }
    if (!repair && !update) throw new Error('Update package is not available. Check for updates again.')
    if (!repair && update && (!update.autoUpdateSupported || update.requiresManualInstall)) {
      throw new Error(update.updateMessage || 'This update requires manual installation from its release page.')
    }
    repairing.value = repair
    status.value = 'downloading'
    resetDownloadProgress()
    appendDownloadLog(repair ? `Preparing application repair ${useAppStore().buildInfoVersion}` : `Preparing update ${update!.currentVersion} -> ${update!.version}`)
    error.value = ''
    installErrorVisible.value = false
    installFailed.value = false
    installInFlight = true
    try {
      const settings = useSettingsStore()
      const endpointOverride = settings.developerMode ? settings.updateEndpointOverride.trim() : ''
      if (repair) await invoke('start_application_repair', { source: settings.updateSource || 'auto' })
      else await invoke('start_managed_update', {
        channel: settings.updateChannel,
        endpointOverride: endpointOverride || null,
        expectedVersion: update!.version,
        source: settings.updateSource || 'auto',
      })
      status.value = 'installing'
      if (repair || update!.distribution === 'macosApp' || update!.distribution === 'appImage') {
        // Unix updaters confirm the replacement has started before exiting this process.
        return true
      }
      appendDownloadLog('Update package staged; restarting application')
      restartRequired.value = true
      lastAcceptedVersion.value = update!.version
      deferredVersion.value = ''
      deferredAt.value = ''
      installErrorVisible.value = false
      installFailed.value = false
      try {
        await persistState()
      } catch (persistError) {
        // The helper is already waiting for this process to exit. A persistence failure must
        // not enter the outer error path and leave the helper blocked indefinitely.
        appendDownloadLog('Update state could not be saved; continuing with restart')
        studioLog.warn('Failed to persist update state after staging update', persistError)
      }
      return await retryUpdateExit()
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err)
      error.value = detail.includes('APPLICATION_UPDATE_BUSY:') ? String(i18n.global.t('settings.updateTasksBusy'))
        : detail.includes('APPLICATION_UPDATE_WINDOWS:') ? String(i18n.global.t('settings.updateCloseEditors'))
        : detail.includes('MACOS_UPDATE_SIGNATURE_INVALID:') ? String(i18n.global.t('settings.updateMacosSignatureInvalid'))
        : detail.includes('MACOS_UPDATE_ROLLBACK_FAILED:') ? String(i18n.global.t('settings.updateRollbackFailed'))
        : detail.includes('MACOS_UPDATE_ROLLED_BACK:') ? String(i18n.global.t('settings.updateRolledBack'))
        : detail.includes('APPLICATION_REPAIR_UNAVAILABLE:') ? String(i18n.global.t('settings.applicationRepairUnavailable')) : detail
      appendDownloadLog(`Update failed: ${detail}`)
      status.value = 'failed'
      installErrorVisible.value = true
      installFailed.value = true
      throw err
    } finally {
      installInFlight = false
    }
  }

  function dismissInstallError() {
    if (restartRequired.value) return
    installErrorVisible.value = false
    repairing.value = false
  }

  async function deferUntilNextLaunch() {
    if (restartRequired.value) return false
    const update = availableUpdate.value
    if (!update) return false
    deferredVersion.value = update.version
    deferredAt.value = new Date().toISOString()
    status.value = 'ready'
    await persistState()
    return true
  }

  watch(currentVersion, (version) => {
    if (!version) return
    if (version === deferredVersion.value) {
      deferredVersion.value = ''
      deferredAt.value = ''
      lastAcceptedVersion.value = version
      void persistState()
      return
    }
    if (version === lastAcceptedVersion.value) return
    if (!lastAcceptedVersion.value) {
      lastAcceptedVersion.value = version
      void persistState()
    }
  })

  function dismiss() {
    if (restartRequired.value) return
    resetResult()
    status.value = 'idle'
  }

  return {
    integrity,
    integrityChecking,
    integrityError,
    repairing,
    checkIntegrity,
    repairApplication,
    status,
    currentVersion,
    latestVersion,
    releaseNotes,
    releaseDate,
    updateMessage,
    manualInstallUrl,
    selectedSource,
    error,
    lastCheckedAt,
    downloadDownloadedBytes,
    downloadTotalBytes,
    downloadSpeedBytesPerSecond,
    downloadEtaSeconds,
    downloadLogs,
    downloadProgressPercent,
    installErrorVisible,
    installFailed,
    restartRequired,
    availableUpdate,
    deferredVersion,
    deferredAt,
    lastAcceptedVersion,
    updateIsPrerelease,
    requiresManualInstall,
    shouldShowDeferred,
    hasPendingDeferredVersion,
    hasUpdate,
    isBusy,
    isInstallingUpdate,
    isInstallOverlayVisible,
    installationBlockedReason,
    initialize,
    checkForUpdates,
    downloadAndInstall,
    retryUpdateExit,
    closeForUpdate,
    dismissInstallError,
    deferUntilNextLaunch,
    dismiss,
  }
})
