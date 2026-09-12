<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import { useDialog, useMessage } from 'naive-ui'
import { invoke } from '@tauri-apps/api/core'
import { open } from '@tauri-apps/plugin-shell'
import packageMeta from '../../package.json'
import appLogo from '@/assets/app-logo-symbol-canvas.png'
import { SYSTEM_LOCALE, setLocale, type LocaleSetting } from '@/i18n'
import { useSettingsStore } from '@/stores/settings'
import { useAppStore, type InstalledRuntime, type RuntimeBackend } from '@/stores/app'
import { useUpdateStore } from '@/stores/update'
import {
  detectRuntimePlatform,
  recommendedRuntimeBackend,
  runtimeAcceleratorReady,
  runtimeCoreUpdateAvailable,
  runtimeCoreSyncAvailable,
  runtimeEnvironmentForBackend,
  runtimeManifestStatus,
  runtimeLoadError,
  runtimeBackendLabel as runtimeBackendName,
  runtimeSizeHint,
  isKnownRuntimeBackend,
} from '@/utils/runtime'
import { useModelStore } from '@/stores/model'
import { useTaskStore } from '@/stores/task'
import { normalizeConcurrentSeparations } from '@/features/tasks/concurrency'
import { formatBytes } from '@/utils/format'
import { formatDateTime } from '@/utils/time'
import { DEFAULT_SCALE_FACTOR, normalizeScaleFactor } from '@/utils/appZoom'
import {
  applyTheme,
  getThemeAccentPreview,
  resolvedIsDark,
  runRippleViewTransition,
  THEME_ACCENTS,
  type ThemeAccent,
  type ThemeMode,
} from '@/utils/theme'
import {
  ColorPaletteOutline,
  FolderOpenOutline,
  TerminalOutline,
  SettingsOutline,
  SpeedometerOutline,
  SwapHorizontalOutline,
  LogoGithub,
  InformationCircleOutline,
  DocumentTextOutline,
  LinkOutline,
  OpenOutline,
  DownloadOutline,
  AlertCircleOutline,
  CheckmarkCircleOutline,
  TrashOutline,
  PlayOutline,
  HardwareChipOutline,
  CloudDownloadOutline,
  RefreshOutline,
} from '@vicons/ionicons5'

const { t, locale: currentLocale } = useI18n()
const message = useMessage()
const dialog = useDialog()
const route = useRoute()
const settings = useSettingsStore()
const app = useAppStore()
const updates = useUpdateStore()
const modelStore = useModelStore()
const task = useTaskStore()
type SettingsSection = 'about' | 'appearance' | 'runtime' | 'paths' | 'defaults'
type BuildInfo = {
  version: string
  gitCommit: string
  gitTag: string
  gitRef: string
  runId: string
  runAttempt: string
  repository: string
  repositoryOwner: string
  buildTime: string
  target: string
  variant: string
  updateSupported?: boolean
  official: boolean
}
const activeSection = ref<SettingsSection>('appearance')
const buildInfo = ref<BuildInfo | null>(null)
const updateChecking = ref(false)
const updateInstalling = ref(false)
const appVersion = computed(() => buildInfo.value?.version || packageMeta.version || '0.0.0')
const repoUrl = 'https://github.com/pymss-project/pymss-studio'
const officialRepository = 'pymss-project/pymss-studio'
const officialRepositoryOwner = 'pymss-project'
const coreRepoUrl = 'https://github.com/pymss-project/pymss'
const licenseUrl = 'https://www.gnu.org/licenses/agpl-3.0.html'
const coreLicenseUrl = 'https://github.com/pymss-project/pymss/blob/main/LICENSE'
const desktopLicense = 'AGPL-3.0'
const coreLicense = 'MIT'
const {
  themeMode,
  themeAccent,
  scaleFactor,
  locale,
  animationsEnabled,
  developerMode,
  dataRoot,
  modelDir,
  outputDir,
  settingsDir,
  editorProjectsDir,
  logsDir,
  defaultDevice,
  downloadSource,
  downloadMethod,
  maxConcurrentSeparations,
  modelDirMigrationState,
  isModelDirMigrating,
  proxyMode,
  proxyUrl,
  proxyBypass,
  updateChannel,
} = storeToRefs(settings)
const { downloadTasks } = storeToRefs(modelStore)
const { activeWorkerTasks } = storeToRefs(task)
const deviceOptions = computed(() => settings.deviceOptions(app.envInfo))
const themeAccentOptions = computed(() =>
  THEME_ACCENTS.map((accent) => ({
    value: accent,
    label: t(`settings.themeAccent${accent[0].toUpperCase()}${accent.slice(1)}`),
    preview: getThemeAccentPreview(accent, resolvedIsDark(themeMode.value)),
  })),
)
const languageOptions = computed(() => [
  { label: t('settings.languageSystem'), value: SYSTEM_LOCALE },
  { label: t('settings.languageSimplifiedChinese'), value: 'zh-CN' },
  { label: t('settings.languageEnglish'), value: 'en' },
])

type ProxyTestStatus = 'idle' | 'testing' | 'success' | 'error'
const proxyTestStatus = ref<ProxyTestStatus>('idle')
const proxyTestMessage = ref('')
const proxyTestSuggestion = ref('')
const proxyTestElapsed = ref(0)
const proxyTestLoading = computed(() => proxyTestStatus.value === 'testing')
const runtimeMirror = ref('auto')
const runtimeDetecting = ref(false)
const runtimeDeleting = ref<string | null>(null)
const runtimeActivating = ref<string | null>(null)
const runtimeLogDialogVisible = ref(false)
const runtimeLogPre = ref<HTMLPreElement | null>(null)

watch(() => app.runtimeInstallLogs.length, () => {
  if (runtimeLogDialogVisible.value && runtimeLogPre.value) {
    nextTick(() => {
      if (runtimeLogPre.value) {
        runtimeLogPre.value.scrollTop = runtimeLogPre.value.scrollHeight
      }
    })
  }
})
const runtimeInstalling = computed(() => app.runtimeInstallStatus === 'installing')
const runtimeCoreUpdating = computed(() => app.runtimeCoreUpdateStatus === 'updating')
// Runtime detection and install/switch/delete all access the runtime state, so only one may run at a time.
const runtimeBusy = computed(() => runtimeDetecting.value || runtimeInstalling.value || runtimeCoreUpdating.value || runtimeActivating.value !== null || runtimeDeleting.value !== null)
const runtimeCurrentError = computed(() => {
  const error = runtimeLoadError(app.runtimeInfo, app.envInfo)
  return error.toLowerCase().includes('winerror 1455') ? t('settings.runtimePageFileError') : error
})
const runtimeCurrentBackend = computed<RuntimeBackend | null>(() => {
  const detected = String(app.runtimeInfo?.torchBackend || app.envInfo?.torchBackend || '')
  if (detected.toLowerCase().startsWith('error:')) return null
  const recorded = String(app.runtimeInfo?.installedBackend || app.runtimeInfo?.installState?.backend || '')
  if (isKnownRuntimeBackend(recorded)) return recorded
  if (app.runtimeInstalledBackend && isKnownRuntimeBackend(app.runtimeInstalledBackend)) return app.runtimeInstalledBackend
  return isKnownRuntimeBackend(detected) ? detected : null
})
const runtimeCurrentLabel = computed(() => {
  if (runtimeCurrentError.value) return t('settings.runtimeLoadFailed')
  return runtimeCurrentBackend.value ? runtimeBackendName(runtimeCurrentBackend.value) : t('settings.envNotChecked')
})
const installedRuntimes = computed(() => app.runtimeInfo?.installedEnvironments || [])
const latestPymssVersion = computed(() => app.runtimeCoreVersions?.packages?.pymss?.latestVersion || null)
const latestPymssCoreVersion = computed(() => app.runtimeCoreVersions?.packages?.['pymss-core']?.latestVersion || null)

type BackendCardState = 'active' | 'installed' | 'not_installed'

type RuntimeBackendCard = {
  backend: RuntimeBackend
  key: string
  label: string
  description: string
  offCatalog?: boolean
  env?: InstalledRuntime
  state: BackendCardState
  recommended: boolean
  manifestOutdated: boolean
  manifestNewer: boolean
  workflowGraphUnavailable: boolean
  health: string
  leftover: boolean
  leftoverLabel: string
  vendorMissing: boolean
  sizeHint: string
  diskLabel: string
  installing: boolean
  pymssVersion: string
  pymssCoreVersion: string
  coreUpdateAvailable: boolean
}

const runtimePlatform = computed(() => detectRuntimePlatform(app.runtimeInfo))
const runtimeRecommendedBackend = computed(() => recommendedRuntimeBackend(app.runtimeInfo))

const RUNTIME_BACKEND_DESC_KEYS: Record<RuntimeBackend, string> = {
  cpu: 'onboarding.runtimeCpu',
  cuda: 'onboarding.runtimeCuda',
  rocm: 'onboarding.runtimeRocm',
  mlx: 'onboarding.runtimeMlx',
}

function runtimeBackendDescription(backend: RuntimeBackend | string) {
  const key = RUNTIME_BACKEND_DESC_KEYS[backend as RuntimeBackend]
  return key ? t(key) : ''
}

type BackendCardBase = { backend: RuntimeBackend; label: string; description: string; offCatalog: boolean }

const runtimeBackendCatalog = computed<BackendCardBase[]>(() => {
  const { isMac, isAppleSilicon } = runtimePlatform.value
  const backends: RuntimeBackend[] = ['cpu']
  if (!isMac) backends.push('cuda', 'rocm')
  if (isAppleSilicon) backends.push('mlx')
  return backends.map((backend) => ({
    backend,
    label: runtimeBackendName(backend),
    description: runtimeBackendDescription(backend),
    offCatalog: false,
  }))
})

const runtimeBackendCards = computed(() => {
  const catalog = [...runtimeBackendCatalog.value]
  // Keep environments that exist on disk but are not offered on this platform
  // (e.g. left over after moving the data root between machines) reachable.
  for (const entry of installedRuntimes.value) {
    const backend = String(entry.backend || '')
    if (!backend || catalog.some((item) => item.backend === backend)) continue
    catalog.push({
      backend: backend as RuntimeBackend,
      label: runtimeBackendName(backend),
      description: runtimeBackendDescription(backend),
      offCatalog: true,
    })
  }
  const cards: RuntimeBackendCard[] = []
  for (const item of catalog) {
    const matchingEnvs = installedRuntimes.value.filter((entry) => entry.backend === item.backend)
    const preferredEnv = runtimeEnvironmentForBackend(app.runtimeInfo, item.backend)
    const envs = preferredEnv
      ? [preferredEnv, ...matchingEnvs.filter((entry) => entry !== preferredEnv)]
      : matchingEnvs
    const displayEnvs = envs.length ? envs : [undefined]

    for (const env of displayEnvs) {
      const isActive = Boolean(env && runtimeCurrentBackend.value === item.backend && runtimeEnvironmentForBackend(app.runtimeInfo, item.backend) === env)
      const state: BackendCardState = isActive ? 'active' : env ? 'installed' : 'not_installed'
      const diskBytes = app.runtimeEnvSizes[String(item.backend)]
      const pymssVersion = env?.pymssVersion || env?.packageVersions?.pymss || ''
      const pymssCoreVersion = env?.pymssCoreVersion || env?.packageVersions?.['pymss-core'] || ''
      const manifestStatus = runtimeManifestStatus(env, app.runtimeInfo?.manifestVersion)
      const manifestOutdated = manifestStatus === 'older' || manifestStatus === 'newer'
      const manifestNewer = manifestStatus === 'newer'
      const workflowGraphUnavailable = env?.pymssGraphAvailable === false
      const health = String(env?.health || 'unknown')
      // A manifest mismatch can mean that the environment was created by an
      // older app even when PyPI already reports the same pymss/core versions.
      // Offer the no-Torch-reinstall core sync so the environment metadata and
      // any manifest-declared pymss extras are brought up to date.
      const coreUpdateAvailable = isActive && env?.coreUpdateSupported !== false && (
        runtimeCoreUpdateAvailable(env, latestPymssVersion.value, latestPymssCoreVersion.value, app.runtimeInfo?.manifestVersion)
        || runtimeCoreSyncAvailable(env, app.runtimeInfo?.manifestVersion)
      )
      const gpuBackend = item.backend === 'cuda' || item.backend === 'rocm' || item.backend === 'mlx'
      // A cancelled or failed install leaves its venv behind without an install state, so the
      // backend reads as not installed while still holding gigabytes. Surface it so the space
      // can be reclaimed instead of being stranded.
      const leftover = !env && app.runtimeIncompleteBackends.includes(String(item.backend))
      cards.push({
        ...item,
        key: `${item.backend}:${env?.pythonPath || 'none'}`,
        env,
        state,
        recommended: runtimeRecommendedBackend.value === item.backend,
        manifestOutdated,
        manifestNewer,
        workflowGraphUnavailable,
        health,
        leftover,
        leftoverLabel: leftover && diskBytes ? formatBytes(diskBytes) : '',
        // Only warn on GPU backends: it explains why a card is there without hiding it, and
        // detection missing a card must never stop someone installing what they need.
        // Off-catalog cards already carry their own "not for this platform" line; a second,
        // near-identical explanation on top of it is just noise.
        vendorMissing: gpuBackend
          && !item.offCatalog
          && runtimeRecommendedBackend.value !== null
          && runtimeRecommendedBackend.value !== item.backend,
        sizeHint: runtimeSizeHint(item.backend),
        // Absent means "not measured", and zero means the walk
        // could not read anything. Neither is a usable number, and formatBytes renders both as
        // an em dash, so only show the row when there is a real size to show.
        diskLabel: diskBytes ? formatBytes(diskBytes) : '',
        installing: runtimeInstalling.value && app.runtimeInstallBackend === item.backend,
        pymssVersion,
        pymssCoreVersion,
        coreUpdateAvailable,
      })
    }
  }
  return cards
})

function runtimeCoreVersionLabel(card: { pymssVersion?: string; pymssCoreVersion?: string }) {
  return t('settings.runtimeCoreVersion', {
    pymss: card.pymssVersion || t('settings.runtimeCoreVersionUnknown'),
    core: card.pymssCoreVersion || t('settings.runtimeCoreVersionUnknown'),
  })
}

// Same naming as the cards ("NVIDIA CUDA", not "CUDA"), so a confirmation dialog never appears
// to be talking about something other than the card that opened it.
function runtimeBackendLabel(value: RuntimeBackend | string | null | undefined) {
  return value ? runtimeBackendName(value) : t('settings.envNotChecked')
}

function confirmRuntimeAction(title: string, content: string, positiveText: string) {
  return new Promise<boolean>((resolve) => {
    dialog.warning({
      title,
      content,
      positiveText,
      negativeText: t('common.cancel'),
      onPositiveClick: () => resolve(true),
      onNegativeClick: () => resolve(false),
      onClose: () => resolve(false),
    })
  })
}

async function installBackend(backend: RuntimeBackend) {
  if (runtimeBusy.value) return
  const target = runtimeBackendLabel(backend)
  const sizeHint = t('settings.runtimeInstallSizeHint', { size: runtimeSizeHint(backend) })
  const reinstall = installedRuntimes.value.some((entry) => entry.backend === backend)
  // The worker writes active-runtime.json at the end of an install, so this also switches.
  const switchHint = runtimeCurrentBackend.value === backend ? '' : ` ${t('settings.runtimeInstallActivatesHint')}`
  const confirmed = await confirmRuntimeAction(
    reinstall ? t('settings.runtimeReinstallTitle') : t('settings.runtimeInstallTitle'),
    reinstall
      ? `${t('settings.runtimeReinstallContent', { current: runtimeBackendLabel(runtimeCurrentBackend.value), target })} ${sizeHint}${switchHint}`
      : `${t('settings.runtimeInstallContent', { backend: target })} ${sizeHint}${switchHint}`,
    t('common.confirm'),
  )
  if (!confirmed) return
  try {
    await app.installRuntime(backend, runtimeMirror.value, currentLocale.value)
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function retryRuntimeInstall() {
  const backend = app.runtimeInstallBackend as RuntimeBackend | null
  if (!backend) {
    message.error(t('settings.runtimeRetryUnavailable'))
    return
  }
  await installBackend(backend)
}

watch(() => app.runtimeInstallStatus, (status, previous) => {
  if (previous !== 'installing') return
  if (status === 'success') message.success(t('settings.runtimeInstallSuccess'))
  // Refresh on failure and cancellation too: an interrupted install leaves its venv behind,
  // and that leftover only becomes visible once sizes are re-measured.
  if (status === 'success' || status === 'error' || status === 'cancelled') {
    void app.loadRuntimeEnvSizes()
  }
})

async function refreshRuntimeStatus(showSuccess = false) {
  runtimeDetecting.value = true
  try {
    await Promise.all([app.checkRuntimeInfo(), app.checkEnv(), app.loadRuntimeEnvSizes()])
    if (showSuccess) message.success(t('settings.runtimeDetectSuccess'))
  } catch (error) {
    if (showSuccess) message.error(error instanceof Error ? error.message : String(error))
  } finally {
    runtimeDetecting.value = false
  }
}

async function refreshRuntimeCoreVersions() {
  try {
    await app.loadRuntimeCoreVersions()
  } catch {
    // Network availability is optional for the settings page; the installed version still shows.
  }
}

// Measuring disk usage walks every file in each venv, so only refresh the full runtime section
// when it is visible.
watch(activeSection, (section) => {
  if (section === 'runtime') {
    void refreshRuntimeStatus(false)
    void refreshRuntimeCoreVersions()
  }
}, { immediate: true })

watch(
  () => [app.runtimeInfo?.installedBackend, app.runtimeInfo?.installState?.backend, app.runtimeInfo?.installedEnvironments?.length],
  () => {
    if (activeSection.value === 'runtime') {
      void refreshRuntimeCoreVersions()
    }
  },
)

const runtimeDiskTotalLabel = computed(() => {
  const values = Object.values(app.runtimeEnvSizes).filter((value) => value > 0)
  // A single environment already shows its own size on the card; a total only adds
  // information once more than one is installed.
  if (values.length < 2) return ''
  return formatBytes(values.reduce((sum, value) => sum + value, 0))
})

// Walking several multi-GB venvs takes visible time on a slow disk; say so rather than
// leaving the section silently size-less.
const runtimeDiskMeasuring = computed(() =>
  app.runtimeEnvSizesLoading && !Object.keys(app.runtimeEnvSizes).length)

async function cancelRuntimeInstall() {
  try {
    await app.cancelRuntimeInstall()
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function deleteRuntime(card: { backend: RuntimeBackend; env?: InstalledRuntime }) {
  if (runtimeBusy.value) return
  const backend = card.backend
  const active = runtimeEnvironmentForBackend(app.runtimeInfo, backend)
  if (card.env && active === card.env && runtimeCurrentBackend.value === backend) {
    message.warning(t('settings.runtimeDeleteActiveError'))
    return
  }
  // Cleaning up an unfinished install discards nothing usable, so it must not be described
  // as losing an environment.
  const leftoverOnly = app.runtimeIncompleteBackends.includes(backend)
    && !installedRuntimes.value.some((entry) => entry.backend === backend)
  const confirmed = await confirmRuntimeAction(
    leftoverOnly ? t('settings.runtimeCleanLeftoverTitle') : t('settings.runtimeDeleteTitle'),
    leftoverOnly
      ? t('settings.runtimeCleanLeftoverContent', { backend: runtimeBackendLabel(backend) })
      : t('settings.runtimeDeleteContent', { backend: runtimeBackendLabel(backend) }),
    leftoverOnly ? t('settings.runtimeCleanLeftover') : t('settings.runtimeDelete'),
  )
  if (!confirmed) return
  runtimeDeleting.value = backend
  try {
    await app.deleteRuntime(backend, {
      pythonPath: card.env?.pythonPath,
    })
    await Promise.all([app.checkRuntimeInfo(), app.checkEnv(), app.loadRuntimeEnvSizes()])
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    runtimeDeleting.value = null
  }
}

async function activateInstalledRuntime(card: { backend: RuntimeBackend; env?: InstalledRuntime }) {
  const backend = card.backend
  const active = runtimeEnvironmentForBackend(app.runtimeInfo, backend)
  if (runtimeBusy.value || (runtimeCurrentBackend.value === backend && active === card.env)) return
  const confirmed = await confirmRuntimeAction(
    t('settings.runtimeSwitchTitle'),
    t('settings.runtimeSwitchContent', {
      current: runtimeBackendLabel(runtimeCurrentBackend.value),
      target: runtimeBackendLabel(backend),
    }),
    t('common.confirm'),
  )
  if (!confirmed) return
  runtimeActivating.value = backend
  try {
    await app.activateRuntime(backend, {
      pythonPath: card.env?.pythonPath,
    })
    message.success(t('settings.runtimeActivateSuccess'))
    await Promise.all([app.checkRuntimeInfo(), app.loadRuntimeCoreVersions()])
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    runtimeActivating.value = null
  }
}

async function detectRuntime() {
  await refreshRuntimeStatus(true)
}

async function updateRuntimeCore(card: { backend: RuntimeBackend; env?: InstalledRuntime }) {
  if (runtimeBusy.value) return
  const runtime = card.env || installedRuntimes.value.find((entry) => entry.backend === card.backend) || null
  const versionUpdateAvailable = runtimeCoreUpdateAvailable(
    runtime || undefined,
    latestPymssVersion.value,
    latestPymssCoreVersion.value,
    app.runtimeInfo?.manifestVersion,
  )
  if (!versionUpdateAvailable && !runtimeCoreSyncAvailable(runtime || undefined, app.runtimeInfo?.manifestVersion)) return
  const confirmed = await confirmRuntimeAction(
    t('settings.runtimeCoreUpdateTitle'),
    t(versionUpdateAvailable ? 'settings.runtimeCoreUpdateContent' : 'settings.runtimeCoreSyncContent', {
      current: runtime?.pymssVersion || runtime?.packageVersions?.pymss || app.runtimeInfo?.pymssVersion || t('settings.runtimeCoreVersionUnknown'),
      latest: latestPymssVersion.value || t('settings.runtimeCoreVersionUnknown'),
      coreCurrent: runtime?.pymssCoreVersion || runtime?.packageVersions?.['pymss-core'] || app.runtimeInfo?.pymssCoreVersion || t('settings.runtimeCoreVersionUnknown'),
      coreLatest: latestPymssCoreVersion.value || t('settings.runtimeCoreVersionUnknown'),
    }),
    t('settings.runtimeCoreUpdate'),
  )
  if (!confirmed) return
  try {
    await app.updateRuntimeCore(card.backend, runtimeMirror.value, currentLocale.value, {
      pythonPath: runtime?.pythonPath,
    })
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

const proxyModeOptions = computed(() => [
  { value: 'none' as const, label: t('settings.proxyModeNone') },
  { value: 'system' as const, label: t('settings.proxyModeSystem') },
  { value: 'custom' as const, label: t('settings.proxyModeCustom') },
])
const downloadMethodOptions = computed(() => [
  { value: 'aria2c' as const, label: t('settings.downloadMethodAria2c') },
  { value: 'urllib' as const, label: t('settings.downloadMethodUrllib') },
])

function buildProxyTestSuggestion(error: string, mode: string): string {
  const lower = error.toLowerCase()
  if (lower.includes('invalid_proxy_url')) {
    return t('settings.proxySuggestionBadScheme')
  }
  if (lower.includes('invalid_proxy_port')) {
    return t('settings.proxySuggestionBadScheme')
  }
  if (lower.includes('unsupported_proxy_scheme')) {
    return t('settings.proxySuggestionBadScheme')
  }
  if (lower.includes('unsupported proxy scheme')) {
    return t('settings.proxySuggestionBadScheme')
  }
  if (lower.includes('pysocks')) {
    return t('settings.proxySuggestionPySocks')
  }
  if (lower.includes('getaddrinfo') || lower.includes('name or service not known') || lower.includes('nodename nor servname')) {
    if (mode === 'none') {
      return t('settings.proxySuggestionDnsNone')
    }
    if (mode === 'system') {
      return t('settings.proxySuggestionDnsSystem')
    }
    return t('settings.proxySuggestionDnsCustom')
  }
  if (lower.includes('timed out') || lower.includes('timeout')) {
    return t('settings.proxySuggestionTimeout')
  }
  if (lower.includes('proxyerror') || lower.includes('407') || lower.includes('proxy')) {
    return t('settings.proxySuggestionProxyError')
  }
  if (lower.includes('connection refused') || lower.includes('connection reset')) {
    return mode === 'custom'
      ? t('settings.proxySuggestionRefusedCustom')
      : t('settings.proxySuggestionRefused')
  }
  return ''
}

async function testProxyConnection() {
  proxyTestStatus.value = 'testing'
  proxyTestMessage.value = t('settings.proxyTesting')
  proxyTestSuggestion.value = ''
  proxyTestElapsed.value = 0
  try {
    const result = await invoke<{
      ok: boolean
      status?: number
      ip?: string
      filesCount?: number
      error?: string
      elapsedMs: number
      mode: string
      proxy?: string
    }>('test_proxy_connection', {
      payload: {
        mode: proxyMode.value,
        url: proxyUrl.value,
        bypass: proxyBypass.value,
        source: downloadSource.value,
        timeout: 15,
      },
    })
    proxyTestElapsed.value = result.elapsedMs
    if (result.ok) {
      proxyTestStatus.value = 'success'
      proxyTestMessage.value = formatConnectionInfo(result)
      proxyTestSuggestion.value = ''
    } else {
      proxyTestStatus.value = 'error'
      const error = result.error || t('settings.proxyTestUnknownError')
      proxyTestMessage.value = error
      proxyTestSuggestion.value = buildProxyTestSuggestion(error, result.mode || proxyMode.value)
    }
  } catch (err) {
    proxyTestStatus.value = 'error'
    const error = err instanceof Error ? err.message : String(err)
    proxyTestMessage.value = error
    proxyTestSuggestion.value = buildProxyTestSuggestion(error, proxyMode.value)
  }
}

function formatConnectionInfo(info: {
  status?: number
  ip?: string
  filesCount?: number
  elapsedMs?: number
}): string {
  const parts: string[] = []
  if (info.status) parts.push(`HTTP ${info.status}`)
  if (info.ip) parts.push(info.ip)
  if (typeof info.filesCount === 'number' && info.filesCount > 0) {
    parts.push(t('settings.proxyTestFiles', { count: info.filesCount }))
  }
  if (info.elapsedMs) parts.push(`${info.elapsedMs}ms`)
  return parts.join('  ·  ')
}

function resetProxyTest() {
  proxyTestStatus.value = 'idle'
  proxyTestMessage.value = ''
  proxyTestSuggestion.value = ''
  proxyTestElapsed.value = 0
}
const settingsSections = computed(() => [
  { key: 'appearance' as const, label: t('settings.appearance'), icon: ColorPaletteOutline, hint: t('settings.appearanceNavHint') },
  { key: 'runtime' as const, label: t('settings.runtime'), icon: TerminalOutline, hint: t('settings.runtimeDesc') },
  { key: 'paths' as const, label: t('settings.dataDir'), icon: FolderOpenOutline, hint: t('settings.pathsNavHint') },
  { key: 'defaults' as const, label: t('settings.defaults'), icon: SettingsOutline, hint: t('settings.defaultsNavHint') },
  { key: 'about' as const, label: t('settings.about'), icon: InformationCircleOutline, hint: t('settings.aboutNavHint') },
])
const pymssCoreVersion = computed(() => {
  if (!app.envInfo) return t('settings.envNotChecked')
  if (!app.envInfo.pymssAvailable) return t('common.notAvailable')
  return app.envInfo.pymssVersion || t('common.unknown')
})
const workerVersion = computed(() => app.envInfo?.workerVersion || t('common.unknown'))
const buildCommitShort = computed(() => buildInfo.value?.gitCommit ? buildInfo.value.gitCommit.slice(0, 7) : '')
const buildRunLabel = computed(() => {
  const info = buildInfo.value
  if (!info?.runId) return ''
  return info.runAttempt ? `#${info.runId}.${info.runAttempt}` : `#${info.runId}`
})
const buildFingerprint = computed(() => {
  const info = buildInfo.value
  if (!info) return t('common.unknown')
  const parts = [info.gitTag || info.gitRef, buildCommitShort.value, info.variant, buildRunLabel.value, info.repository].filter(Boolean)
  return parts.length ? parts.join(' · ') : t('settings.buildFingerprintUnavailable')
})

watch(() => app.runtimeCoreUpdateStatus, (status, previous) => {
  if (previous !== 'updating') return
  if (status === 'success') message.success(t('settings.runtimeCoreUpdateSuccess'))
})
function isOfficialRepositoryBuild(info: BuildInfo) {
  return info.official
    && info.repository === officialRepository
    && info.repositoryOwner === officialRepositoryOwner
    && Boolean(info.gitCommit && info.runId)
}
const buildVerification = computed(() => {
  const info = buildInfo.value
  if (!info) {
    return {
      label: t('settings.buildStatusUnknown'),
      tone: 'unknown',
      description: t('settings.buildStatusUnknownDesc'),
    }
  }
  if (isOfficialRepositoryBuild(info)) {
    return {
      label: t('settings.buildStatusOfficial'),
      tone: 'official',
      description: t('settings.buildStatusOfficialDesc'),
    }
  }
  if (info.gitCommit && info.runId) {
    return {
      label: t('settings.buildStatusCi'),
      tone: 'development',
      description: t('settings.buildStatusCiDesc'),
    }
  }
  return {
    label: t('settings.buildStatusDevelopment'),
    tone: 'development',
    description: t('settings.buildStatusDevelopmentDesc'),
  }
})
const aboutVersionItems = computed(() => [
  { label: t('settings.softwareVersion'), value: appVersion.value, meta: 'Pymss Studio' },
  { label: t('settings.pymssCoreVersion'), value: pymssCoreVersion.value, meta: t('settings.coreRuntime') },
  { label: t('settings.workerVersion'), value: workerVersion.value, meta: t('settings.pythonWorker') },
])
const updateStatusLabel = computed(() => {
  if (!updateSupported.value) return t('settings.updateUnsupported')
  if (updates.requiresManualInstall) return t('settings.updateManualInstallTitle')
  if (updates.status === 'failed' && updates.installFailed) return t('settings.updateInstallFailed')
  if (updates.shouldShowDeferred) return t('settings.updateDeferred')
  if (updates.status === 'checking') return t('settings.updateChecking')
  if (updates.status === 'downloading') return t('settings.updateDownloading')
  if (updates.status === 'installing') return t('settings.updateInstalling')
  if (updates.status === 'available') return t('settings.updateAvailable')
  if (updates.status === 'failed') return t('settings.updateCheckFailed')
  return t('settings.updateIdle')
})
const updateBadgeTone = computed(() => {
  if (!updateSupported.value) return 'default'
  if (updates.status === 'failed') return 'error'
  if (updates.shouldShowDeferred) return 'warning'
  if (updates.status === 'available') return 'success'
  if (updates.status === 'checking' || updates.status === 'downloading' || updates.status === 'installing') return 'warning'
  return 'default'
})
const updateSupported = computed(() => {
  return buildInfo.value?.updateSupported === true || app.buildInfoUpdateSupported
})
const updateLastCheckedLabel = computed(() => {
  return formatDateTime(updates.lastCheckedAt) || t('settings.updateNeverChecked')
})
const runtimeSummaryItems = computed(() => [
  { label: t('settings.runtimeCurrentLabel'), value: runtimeCurrentLabel.value, meta: t('settings.runtimeCurrentMeta') },
  { label: 'Torch', value: app.runtimeInfo?.torchVersion || app.envInfo?.torchVersion || t('common.unknown'), meta: t('settings.runtimeTorchMeta') },
  { label: 'pymss', value: app.envInfo?.pymssVersion || t('common.unknown'), meta: t('settings.runtimePymssMeta') },
])
const updateReleaseDateLabel = computed(() => {
  return formatDateTime(updates.releaseDate)
})
const updateChannelOptions = computed(() => [
  { label: t('settings.updateChannelStable'), value: 'stable' },
  { label: t('settings.updateChannelPrerelease'), value: 'prerelease' },
])
const aboutLinks = computed(() => [
  { label: t('settings.desktopRepository'), url: repoUrl, icon: LogoGithub },
  { label: t('settings.coreRepository'), url: coreRepoUrl, icon: LinkOutline },
  { label: t('settings.licenseLink'), url: licenseUrl, icon: DocumentTextOutline },
  { label: t('settings.coreLicenseLink'), url: coreLicenseUrl, icon: DocumentTextOutline },
])

async function loadBuildInfo() {
  try {
    buildInfo.value = await invoke<BuildInfo>('get_build_info')
    app.buildInfoVersion = buildInfo.value?.version || ''
    app.buildInfoVariant = buildInfo.value?.variant || ''
    app.buildInfoUpdateSupported = buildInfo.value?.updateSupported === true
  } catch {
    buildInfo.value = null
  }
}

async function checkForUpdates(manual = false) {
  if (updateChecking.value) return
  updateChecking.value = true
  try {
    await updates.checkForUpdates(manual)
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    updateChecking.value = false
  }
}

async function changeUpdateChannel(value: 'stable' | 'prerelease') {
  if (value === updateChannel.value) return
  if (value === 'prerelease') {
    const confirmed = await confirmRuntimeAction(
      t('settings.updateChannelPrereleaseTitle'),
      t('settings.updateChannelPrereleaseContent'),
      t('settings.updateChannelPrereleaseConfirm'),
    )
    if (!confirmed) return
  }
  updateChannel.value = value
  updates.dismiss()
  if (updateSupported.value) await checkForUpdates(true)
}

async function installUpdate() {
  if (updateInstalling.value || !updates.hasUpdate) return
  if (updates.shouldShowDeferred && updates.latestVersion) {
    const confirmed = await confirmRuntimeAction(
      t('settings.updateInstallTitle'),
      updates.updateIsPrerelease
        ? t('settings.updateInstallPrereleaseDeferredContent', { version: updates.latestVersion })
        : t('settings.updateInstallDeferredContent', { version: updates.latestVersion }),
      t('settings.updateInstallConfirm'),
    )
    if (!confirmed) return
    try {
      updateInstalling.value = true
      await updates.downloadAndInstall()
      message.success(t('settings.updateDeferredSaved'))
      return
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error))
      return
    } finally {
      updateInstalling.value = false
    }
  }
  const confirmed = await confirmRuntimeAction(
    t('settings.updateInstallTitle'),
    updates.updateIsPrerelease
      ? t('settings.updateInstallPrereleaseContent', { version: updates.latestVersion || appVersion.value })
      : t('settings.updateInstallContent', { version: updates.latestVersion || appVersion.value }),
    t('settings.updateInstallConfirm'),
  )
  if (!confirmed) return
  updateInstalling.value = true
  try {
    await updates.downloadAndInstall()
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    updateInstalling.value = false
  }
}

const SCALE_FACTOR_PRESET_VALUES = [0.75, 0.9, 1, 1.1, 1.25, 1.5] as const
const scaleFactorPercent = computed(() => formatScaleFactorLabel(scaleFactor.value))
const scaleSliderIndex = computed({
  get: () => {
    const current = normalizeScaleFactor(scaleFactor.value)
    const exact = SCALE_FACTOR_PRESET_VALUES.findIndex((value) => isSameScaleFactor(value, current))
    if (exact !== -1) return exact
    let nearest = 0
    let minDiff = Number.POSITIVE_INFINITY
    SCALE_FACTOR_PRESET_VALUES.forEach((value, index) => {
      const diff = Math.abs(value - current)
      if (diff < minDiff) {
        minDiff = diff
        nearest = index
      }
    })
    return nearest
  },
  set: (index: number) => {
    const value = SCALE_FACTOR_PRESET_VALUES[index]
    if (value !== undefined) updateScaleFactor(value)
  },
})
const scaleSliderMarks = computed<Record<number, string>>(() =>
  SCALE_FACTOR_PRESET_VALUES.reduce<Record<number, string>>((marks, value, index) => {
    marks[index] = formatScaleFactorLabel(value)
    return marks
  }, {}),
)
const isDefaultScaleFactor = computed(() => isSameScaleFactor(scaleFactor.value, DEFAULT_SCALE_FACTOR))
const maxConcurrentSeparationsInput = computed({
  get: () => normalizeConcurrentSeparations(maxConcurrentSeparations.value),
  set: (value) => {
    maxConcurrentSeparations.value = normalizeConcurrentSeparations(value)
  },
})
const dataDirEntries = computed(() => [
  { key: 'settings.editorProjectsDir', value: editorProjectsDir.value, fallback: 'editor-projects' },
  { key: 'settings.settingsDir', value: settingsDir.value, fallback: 'settings' },
  { key: 'settings.logsDir', value: logsDir.value, fallback: 'logs' },
])
const hasActiveModelDirUsage = computed(() => {
  const hasRunningWorkerTask = activeWorkerTasks.value.length > 0
  const hasDownloadingModel = Object.values(downloadTasks.value).some((item) => ['preparing', 'downloading'].includes(item.status))
  return hasRunningWorkerTask || hasDownloadingModel || isModelDirMigrating.value
})
const currentResolvedLanguageLabel = computed(() =>
  currentLocale.value === 'en'
    ? t('settings.languageEnglish')
    : t('settings.languageSimplifiedChinese'),
)
const appearanceThemeAccentLabel = computed(() =>
  t(`settings.themeAccent${themeAccent.value[0].toUpperCase()}${themeAccent.value.slice(1)}`),
)
const modelDirMigrationVisible = computed(() => modelDirMigrationState.value.status !== 'idle' && modelDirMigrationState.value.status !== 'confirm')
const modelDirMigrationProgress = computed(() => {
  const state = modelDirMigrationState.value
  if (['ready_to_switch', 'finalizing_cleanup', 'success'].includes(state.status)) return 100
  if (state.totalBytes > 0) return Math.max(0, Math.min(99, Math.round((state.copiedBytes / state.totalBytes) * 100)))
  if (state.totalFiles > 0) return Math.max(0, Math.min(99, Math.round((state.completedFiles / state.totalFiles) * 100)))
  return 0
})
const modelDirMigrationHasConflict = computed(() => modelDirMigrationState.value.status === 'conflict' && !!modelDirMigrationState.value.conflict)
const modelDirMigrationHasResult = computed(() => ['success', 'failed', 'aborted'].includes(modelDirMigrationState.value.status))
const isCheckingModelDir = ref(false)
const languageSelectWrap = ref<HTMLElement | null>(null)

function dirName(path: string, fallback: string) {
  const normalized = (path || '').trim().replace(/[\\/]+$/, '')
  if (!normalized) return fallback
  const segments = normalized.split(/[\\/]+/).filter(Boolean)
  return segments.at(-1) || fallback
}

function getEventOrigin(event: MouseEvent) {
  const target = event.currentTarget as HTMLElement | null
  if (!target) return { x: window.innerWidth / 2, y: window.innerHeight / 2 }
  const rect = target.getBoundingClientRect()
  return {
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
  }
}

function getElementOrigin(element: HTMLElement | null) {
  if (!element) return null
  const rect = element.getBoundingClientRect()
  return {
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
  }
}

function formatScaleFactorLabel(value: number) {
  return `${Math.round(normalizeScaleFactor(value) * 100)}%`
}

function isSameScaleFactor(left: unknown, right: unknown) {
  return Math.abs(normalizeScaleFactor(left) - normalizeScaleFactor(right)) < 0.001
}

function updateScaleFactor(value: number) {
  scaleFactor.value = normalizeScaleFactor(value)
}

function resetScaleFactorToDefault() {
  updateScaleFactor(DEFAULT_SCALE_FACTOR)
}

async function selectThemeMode(mode: ThemeMode, event: MouseEvent) {
  if (themeMode.value === mode) return
  const origin = animationsEnabled.value ? getEventOrigin(event) : undefined
  await runRippleViewTransition(() => {
    themeMode.value = mode
    applyTheme(mode, themeAccent.value)
  }, origin)
}

async function selectThemeAccent(accent: ThemeAccent, event: MouseEvent) {
  if (themeAccent.value === accent) return
  const origin = animationsEnabled.value ? getEventOrigin(event) : undefined
  await runRippleViewTransition(() => {
    themeAccent.value = accent
    applyTheme(themeMode.value, accent)
  }, origin)
}

async function selectLocale(code: LocaleSetting) {
  if (locale.value === code) return
  const origin = animationsEnabled.value
    ? (getElementOrigin(languageSelectWrap.value) || {
        x: window.innerWidth / 2,
        y: window.innerHeight / 2,
      })
    : undefined
  await runRippleViewTransition(() => {
    locale.value = code
    setLocale(code)
  }, origin)
}

async function revealPath(path: string) {
  try {
    await task.revealPath(path)
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function changeModelDir() {
  if (hasActiveModelDirUsage.value) {
    message.warning(t('settings.modelDirChangeBlocked'))
    return
  }
  const folder = await settings.pickModelDir()
  if (!folder) return
  isCheckingModelDir.value = true
  try {
    const result = await settings.prepareModelDirChange(folder)
    if (result.outcome === 'noop') {
      message.info(t('settings.modelDirSamePath'))
      return
    }
    if (result.outcome === 'switched') {
      message.success(t('settings.modelDirChanged'))
    }
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    isCheckingModelDir.value = false
  }
}

async function changeOutputDir() {
  try {
    await settings.pickOutputDir()
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function openExternalUrl(url: string) {
  try {
    await open(url)
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}


function closeModelDirMigrationDialog() {
  if (modelDirMigrationState.value.status === 'confirm') {
    settings.cancelModelDirChangeConfirmation()
    return
  }
  if (modelDirMigrationHasResult.value) {
    settings.clearModelDirMigrationState()
  }
}

async function confirmModelDirMigration() {
  try {
    await settings.confirmModelDirMigration()
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

async function resolveModelDirConflict(action: 'overwrite' | 'skip' | 'abort') {
  try {
    await settings.resolveModelDirConflict(action)
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}

onMounted(async () => {
  if (route.query.section === 'runtime') activeSection.value = 'runtime'
  await loadBuildInfo()
  if (updateSupported.value) {
    void checkForUpdates(false)
  }
  if (!app.envInfo && !app.envLoading) {
    app.checkEnvInBackground().catch(() => {})
  }
})

</script>

<template>
  <div class="page page--settings">
    <div class="page-header-compact">
      <div class="page-header-compact__main">
        <h1>{{ t('settings.title') }}</h1>
        <p>{{ t('settings.subtitle') }}</p>
      </div>
    </div>

    <div class="settings-shell">
      <aside class="settings-sidebar" :aria-label="t('settings.settingsNavigation')">
        <button
          v-for="section in settingsSections"
          :key="section.key"
          type="button"
          class="settings-nav-item"
          :class="{ active: activeSection === section.key }"
          @click="activeSection = section.key"
        >
          <span class="settings-nav-item__icon"><n-icon :component="section.icon" size="18" /></span>
          <span class="settings-nav-item__copy">
            <strong>{{ section.label }}</strong>
            <small>{{ section.hint }}</small>
          </span>
        </button>
      </aside>

      <main class="settings-content">
        <section v-if="activeSection === 'about'" class="about-panel">
          <article class="about-hero">
            <div class="about-hero__main">
              <div class="about-logo-wrap">
                <img class="about-logo" :src="appLogo" alt="Pymss Studio" />
              </div>
              <div class="about-hero__copy">
                <span class="about-eyebrow">{{ t('settings.aboutEyebrow') }}</span>
                <h2 class="about-hero__title">Pymss Studio</h2>
              </div>
            </div>

            <div class="about-stats" :aria-label="t('settings.about')">
              <div v-for="item in aboutVersionItems" :key="item.label" class="about-stat">
                <span class="about-stat__label">{{ item.label }}</span>
                <strong class="about-stat__value">{{ item.value }}</strong>
                <small class="about-stat__meta">{{ item.meta }}</small>
              </div>
            </div>
          </article>

          <article class="about-build-card" :class="`about-build-card--${buildVerification.tone}`">
            <div class="about-build-card__head">
              <span>{{ t('settings.officialVerification') }}</span>
              <strong>{{ buildVerification.label }}</strong>
            </div>
            <p>{{ buildVerification.description }}</p>
            <div class="about-build-fingerprint">
              <span>{{ t('settings.buildFingerprint') }}</span>
              <code>{{ buildFingerprint }}</code>
            </div>
          </article>

          <article class="about-info-card about-info-card--update">
            <div class="section-title section-title--plain">
              <span class="section-title__icon">
                <n-icon :component="RefreshOutline" size="18" />
              </span>
              <span>{{ t('settings.update') }}</span>
            </div>
            <div class="update-panel">
              <div class="update-panel__overview">
                <div class="update-panel__headline">
                  <span class="update-panel__status-dot" :class="`update-panel__status-dot--${updateBadgeTone}`" aria-hidden="true" />
                  <div>
                    <strong>{{ updateStatusLabel }}</strong>
                    <p>{{ !updateSupported ? t('settings.updateUnsupportedHint') : updates.requiresManualInstall ? (updates.updateMessage || t('settings.updateManualInstallPrompt', { version: updates.latestVersion })) : t('settings.updateStatusHint') }}</p>
                  </div>
                </div>
                <div v-if="updates.updateIsPrerelease" class="update-panel__tags">
                  <n-tag type="warning" size="small">{{ t('settings.updatePrereleaseBadge') }}</n-tag>
                </div>
              </div>
              <div class="update-panel__version-flow">
                <div class="update-panel__version-card update-panel__version-card--current">
                  <span>{{ t('settings.updateCurrentVersionLabel') }}</span>
                  <strong>{{ appVersion }}</strong>
                </div>
                <span class="update-panel__version-arrow" aria-hidden="true">→</span>
                <div class="update-panel__version-card" :class="{ 'update-panel__version-card--target': updates.latestVersion }">
                  <span>{{ updates.latestVersion ? t('settings.updateAvailable') : t('settings.updateLatestVersion') }}</span>
                  <strong>{{ updates.latestVersion || appVersion }}</strong>
                </div>
              </div>
              <div class="update-panel__meta">
                <span>{{ t('settings.updateLastChecked', { time: updateLastCheckedLabel }) }}</span>
                <span v-if="updateReleaseDateLabel">{{ t('settings.updateReleaseDate', { time: updateReleaseDateLabel }) }}</span>
              </div>
              <div class="update-panel__channel">
                <span>{{ t('settings.updateChannelLabel') }}</span>
                <n-select
                  :value="updateChannel"
                  :options="updateChannelOptions"
                  :disabled="!updateSupported || updates.isBusy"
                  size="small"
                  @update:value="changeUpdateChannel"
                />
              </div>
              <p class="update-panel__notes update-panel__notes--muted">{{ updateChannel === 'prerelease' ? t('settings.updateChannelPrereleaseHint') : t('settings.updateChannelStableHint') }}</p>
              <div class="update-panel__release-notes">
                <span>{{ t('settings.updateReleaseNotesLabel') }}</span>
                <p v-if="updates.releaseNotes" class="update-panel__notes">{{ updates.releaseNotes }}</p>
                <p v-else class="update-panel__notes update-panel__notes--muted">{{ t('settings.updateNoNotes') }}</p>
              </div>
              <div class="update-panel__actions">
                <n-button secondary :loading="updateChecking" :disabled="!updateSupported || updates.isBusy" @click="checkForUpdates(true)">
                  <template #icon>
                    <n-icon :component="RefreshOutline" />
                  </template>
                  {{ t('settings.checkForUpdates') }}
                </n-button>
                <n-button v-if="!updates.requiresManualInstall" type="primary" :loading="updateInstalling" :disabled="!updateSupported || !updates.hasUpdate || updates.isBusy" @click="installUpdate">
                  <template #icon>
                    <n-icon :component="CloudDownloadOutline" />
                  </template>
                  {{ t('settings.installUpdate') }}
                </n-button>
                <n-button v-else type="warning" @click="openExternalUrl(updates.manualInstallUrl || repoUrl + '/releases/latest')">
                  <template #icon>
                    <n-icon :component="OpenOutline" />
                  </template>
                  {{ t('settings.updateOpenGitHub') }}
                </n-button>
              </div>
            </div>
          </article>

          <div class="about-detail-grid">
            <article class="about-info-card about-info-card--license">
              <div class="section-title section-title--plain">
                <span class="section-title__icon">
                  <n-icon :component="DocumentTextOutline" size="18" />
                </span>
                <span>{{ t('settings.license') }}</span>
              </div>
              <div class="license-stack">
                <div class="license-row">
                  <span>{{ t('settings.desktopLicense') }}</span>
                  <strong>{{ desktopLicense }}</strong>
                </div>
                <div class="license-row">
                  <span>{{ t('settings.coreLicense') }}</span>
                  <strong>{{ coreLicense }}</strong>
                </div>
              </div>
            </article>

            <article class="about-info-card about-info-card--links">
              <div class="section-title section-title--plain">
                <span class="section-title__icon">
                  <n-icon :component="LinkOutline" size="18" />
                </span>
                <span>{{ t('settings.relatedLinks') }}</span>
              </div>
              <div class="about-link-list">
                <button
                  v-for="link in aboutLinks"
                  :key="link.url"
                  type="button"
                  class="about-link-item"
                  @click="openExternalUrl(link.url)"
                >
                  <span class="about-link-item__icon"><n-icon :component="link.icon" size="18" /></span>
                  <span class="about-link-item__label">{{ link.label }}</span>
                  <n-icon class="about-link-item__open" :component="OpenOutline" size="16" />
                </button>
              </div>
            </article>
          </div>
        </section>

        <!-- Appearance -->
        <section v-else-if="activeSection === 'appearance'" class="settings-section-panel">
        <n-card class="settings-card settings-card--compact settings-card--appearance" :bordered="true" size="small">
          <template #header>
            <div class="section-title">
              <span class="section-title__icon">
                <n-icon :component="ColorPaletteOutline" size="18" />
              </span>
              <span>{{ t('settings.appearance') }}</span>
            </div>
          </template>

          <div class="appearance-list">
            <p class="appearance-hint">{{ t('settings.appearanceHint') }}</p>

            <div class="setting-row">
              <div class="setting-row__label">
                <label class="setting-row__title">{{ t('settings.theme') }}</label>
              </div>
              <div class="setting-row__control">
                <div class="theme-switcher">
                  <button
                    type="button"
                    :class="{ active: themeMode === 'system' }"
                    @click="selectThemeMode('system', $event)"
                  >
                    {{ t('settings.themeSystem') }}
                  </button>
                  <button
                    type="button"
                    :class="{ active: themeMode === 'dark' }"
                    @click="selectThemeMode('dark', $event)"
                  >
                    {{ t('settings.themeDark') }}
                  </button>
                  <button
                    type="button"
                    :class="{ active: themeMode === 'light' }"
                    @click="selectThemeMode('light', $event)"
                  >
                    {{ t('settings.themeLight') }}
                  </button>
                </div>
              </div>
            </div>

            <div class="setting-row">
              <div class="setting-row__label">
                <label class="setting-row__title">{{ t('settings.themeColor') }}</label>
              </div>
              <div class="setting-row__control">
                <div class="accent-dots">
                  <button
                    v-for="accent in themeAccentOptions"
                    :key="accent.value"
                    type="button"
                    class="accent-dot"
                    :class="{ active: themeAccent === accent.value }"
                    :title="accent.label"
                    :aria-label="accent.label"
                    @click="selectThemeAccent(accent.value, $event)"
                  >
                    <span
                      class="accent-dot__fill"
                      :style="{ background: `linear-gradient(135deg, ${accent.preview[0]} 0 50%, ${accent.preview[1]} 50% 100%)` }"
                    />
                  </button>
                  <span class="accent-current">{{ appearanceThemeAccentLabel }}</span>
                </div>
              </div>
            </div>

            <div class="setting-row">
              <div class="setting-row__label">
                <label class="setting-row__title">{{ t('settings.language') }}</label>
                <p v-if="locale === SYSTEM_LOCALE" class="setting-row__hint">
                  {{ t('settings.languageFollowSystemHint', { locale: currentResolvedLanguageLabel }) }}
                </p>
              </div>
              <div class="setting-row__control">
                <div ref="languageSelectWrap" class="language-select-wrap">
                  <n-select
                    :value="locale"
                    :options="languageOptions"
                    :consistent-menu-width="false"
                    @update:value="selectLocale"
                  />
                </div>
              </div>
            </div>

            <div class="setting-row">
              <div class="setting-row__label">
                <label class="setting-row__title">{{ t('settings.scaleFactor') }}</label>
                <button
                  type="button"
                  class="scale-reset"
                  :disabled="isDefaultScaleFactor"
                  @click="resetScaleFactorToDefault"
                >
                  {{ t('settings.restoreDefaultScale') }}
                </button>
              </div>
              <div class="setting-row__control">
                <div class="scale-control">
                  <n-slider
                    v-model:value="scaleSliderIndex"
                    :min="0"
                    :max="5"
                    :step="1"
                    :marks="scaleSliderMarks"
                    :tooltip="false"
                  />
                  <span class="scale-value">{{ scaleFactorPercent }}</span>
                </div>
              </div>
            </div>

            <div class="setting-row">
              <div class="setting-row__label">
                <label class="setting-row__title">{{ t('settings.animations') }}</label>
                <p class="setting-row__hint">{{ t('settings.animationsHint') }}</p>
              </div>
              <div class="setting-row__control">
                <n-switch v-model:value="animationsEnabled" />
              </div>
            </div>
          </div>
        </n-card>
        </section>

        <!-- Runtime -->
        <section v-else-if="activeSection === 'runtime'" class="settings-section-panel runtime-section">
          <article class="about-hero runtime-hero">
            <div class="about-hero__main">
              <div class="section-title__icon runtime-hero__icon">
                <n-icon :component="TerminalOutline" size="18" />
              </div>
              <div class="about-hero__copy">
                <span class="about-eyebrow">{{ t('settings.runtime') }}</span>
                <h2 class="about-hero__title">{{ runtimeCurrentLabel }}</h2>
                <p v-if="runtimeCurrentError" class="runtime-hero__desc runtime-hero__desc--error">{{ runtimeCurrentError }}</p>
                <p v-else class="runtime-hero__desc">{{ t('settings.runtimeDesc') }}</p>
              </div>
              <n-button size="small" secondary :loading="runtimeDetecting" :disabled="runtimeBusy" @click="detectRuntime" style="margin-left: auto; align-self: center;">
                {{ t('settings.runtimeDetect') }}
              </n-button>
            </div>

            <div class="about-stats" :aria-label="t('settings.runtime')">
              <div v-for="item in runtimeSummaryItems" :key="item.label" class="about-stat">
                <span class="about-stat__label">{{ item.label }}</span>
                <strong class="about-stat__value">{{ item.value }}</strong>
                <small class="about-stat__meta">{{ item.meta }}</small>
              </div>
            </div>
          </article>

          <!-- Runtime Environments -->
          <article class="about-info-card runtime-envs-card">
            <div class="section-title section-title--plain">
              <span class="section-title__icon">
                <n-icon :component="HardwareChipOutline" size="18" />
              </span>
              <span>{{ t('settings.runtimeBackendsSection') }}</span>
            </div>
            <div class="runtime-envs-list">
              <div
                v-for="card in runtimeBackendCards"
                :key="card.key"
                class="runtime-env-card"
                :class="{
                  'runtime-env-card--active': card.state === 'active',
                  'runtime-env-card--muted': card.state === 'not_installed',
                }"
              >
                <div class="runtime-env-card__head">
                  <strong class="runtime-env-card__backend">{{ card.label }}</strong>
                  <n-tag v-if="card.state === 'active'" :bordered="false" size="small" type="success" round>
                    {{ t('settings.runtimeActive') }}
                  </n-tag>
                  <n-tag v-else-if="card.state === 'installed'" :bordered="false" size="small" type="info" round>
                    {{ t('settings.runtimeInstalled') }}
                  </n-tag>
                  <n-tag v-if="card.health === 'degraded'" :bordered="false" size="small" type="warning" round>
                    {{ t('settings.runtimeHealthDegraded') }}
                  </n-tag>
                  <n-tag v-else-if="card.health === 'broken'" :bordered="false" size="small" type="error" round>
                    {{ t('settings.runtimeHealthBroken') }}
                  </n-tag>
                  <n-tag v-if="card.recommended" :bordered="false" size="small" type="warning" round>
                    {{ t('settings.runtimeRecommended') }}
                  </n-tag>
                  <n-tag v-if="card.manifestOutdated" :bordered="false" size="small" type="warning" round>
                    {{ t('settings.runtimeManifestOutdated') }}
                  </n-tag>
                  <n-tag v-if="card.workflowGraphUnavailable" :bordered="false" size="small" type="warning" round>
                    {{ t('settings.runtimeWorkflowSupportOutdated') }}
                  </n-tag>
                </div>

                <p v-if="card.description" class="runtime-env-card__desc">{{ card.description }}</p>
                <p v-if="card.offCatalog" class="runtime-env-card__desc">{{ t('settings.runtimeBackendUnsupported') }}</p>
                <p v-if="card.vendorMissing" class="runtime-env-card__desc">{{ t('settings.runtimeVendorNotDetected') }}</p>
                <p v-if="card.health === 'broken'" class="runtime-env-card__desc">{{ t('settings.runtimeHealthBrokenHint') }}</p>
                <p v-if="card.manifestOutdated" class="runtime-env-card__desc">
                  {{ card.env?.coreUpdateSupported === false
                    ? t(card.manifestNewer ? 'settings.runtimeManifestNewerBundledHint' : 'settings.runtimeManifestOutdatedBundledHint')
                    : t(card.manifestNewer ? 'settings.runtimeManifestNewerHint' : 'settings.runtimeManifestOutdatedHint') }}
                </p>
                <p v-if="card.workflowGraphUnavailable" class="runtime-env-card__desc">
                  {{ card.env?.coreUpdateSupported === false
                    ? t('settings.runtimeWorkflowSupportBundledHint')
                    : t('settings.runtimeWorkflowSupportHint') }}
                </p>

                <div v-if="card.env" class="runtime-env-card__meta">
                  <span v-if="card.env.torchVersion">{{ t('settings.runtimeTorchVersion', { version: card.env.torchVersion }) }}</span>
                  <span>{{ runtimeCoreVersionLabel(card) }}</span>
                  <span v-if="card.coreUpdateAvailable" class="runtime-env-card__accel runtime-env-card__accel--warn">
                    {{ t('settings.runtimeCoreUpdateAvailable', {
                      pymss: latestPymssVersion || t('settings.runtimeCoreVersionUnknown'),
                      core: latestPymssCoreVersion || t('settings.runtimeCoreVersionUnknown'),
                    }) }}
                  </span>
                  <span v-if="runtimeAcceleratorReady(card.env, card.backend)" class="runtime-env-card__accel runtime-env-card__accel--ok">{{ t('settings.runtimeAcceleratorAvailable') }}</span>
                  <span v-else-if="card.backend !== 'cpu'" class="runtime-env-card__accel">{{ t('settings.runtimeAcceleratorUnavailable') }}</span>
                  <span v-if="card.diskLabel">{{ t('settings.runtimeDiskUsage', { size: card.diskLabel }) }}</span>
                </div>
                <div v-else-if="card.state === 'not_installed'" class="runtime-env-card__meta">
                  <span>{{ t('settings.runtimeInstallSizeHint', { size: card.sizeHint }) }}</span>
                  <span v-if="card.leftoverLabel" class="runtime-env-card__accel">
                    {{ t('settings.runtimeLeftover', { size: card.leftoverLabel }) }}
                  </span>
                </div>

                <div class="runtime-env-card__actions">
                  <n-button
                    v-if="card.state === 'not_installed'"
                    size="tiny"
                    secondary
                    type="primary"
                    :loading="card.installing"
                    :disabled="runtimeBusy"
                    @click="installBackend(card.backend)"
                  >
                    <template #icon><n-icon :component="DownloadOutline" /></template>
                    {{ card.installing ? t('settings.runtimeInstalling') : t('settings.runtimeInstallBackend') }}
                  </n-button>
                  <n-button
                    v-if="card.state === 'installed' && card.health !== 'broken'"
                    size="tiny"
                    secondary
                    type="primary"
                    :loading="runtimeActivating === card.backend"
                    :disabled="runtimeBusy"
                    @click="activateInstalledRuntime(card)"
                  >
                    <template #icon><n-icon :component="PlayOutline" /></template>
                    {{ t('settings.runtimeActivateShort') }}
                  </n-button>
                  <n-button
                    v-if="card.state !== 'not_installed'"
                    size="tiny"
                    tertiary
                    :loading="card.installing"
                    :disabled="runtimeBusy"
                    @click="installBackend(card.backend)"
                  >
                    {{ card.installing ? t('settings.runtimeInstalling') : t('settings.runtimeRepair') }}
                  </n-button>
                  <n-button
                    v-if="card.coreUpdateAvailable && card.state === 'active'"
                    size="tiny"
                    secondary
                    type="warning"
                    :loading="runtimeCoreUpdating"
                    :disabled="runtimeBusy"
                    @click="updateRuntimeCore(card)"
                  >
                    <template #icon><n-icon :component="CloudDownloadOutline" /></template>
                    {{ t('settings.runtimeCoreUpdate') }}
                  </n-button>
                  <n-button
                    v-if="card.env?.logPath"
                    size="tiny"
                    tertiary
                    @click="revealPath(card.env.logPath)"
                  >
                    {{ t('settings.runtimeViewLog') }}
                  </n-button>
                  <n-button
                    v-if="card.leftover"
                    size="tiny"
                    tertiary
                    type="error"
                    :loading="runtimeDeleting === card.backend"
                    :disabled="runtimeBusy"
                    @click="deleteRuntime(card)"
                  >
                    <template #icon><n-icon :component="TrashOutline" /></template>
                    {{ t('settings.runtimeCleanLeftover') }}
                  </n-button>
                  <n-button
                    v-if="card.state === 'installed' && card.env?.source !== 'bundled'"
                    size="tiny"
                    tertiary
                    type="error"
                    :loading="runtimeDeleting === card.backend"
                    :disabled="runtimeBusy"
                    @click="deleteRuntime(card)"
                  >
                    <template #icon><n-icon :component="TrashOutline" /></template>
                    {{ t('settings.runtimeDelete') }}
                  </n-button>
                </div>

                <div v-if="card.installing" class="runtime-env-card__progress">
                  <n-spin size="small" />
                  <span class="runtime-env-card__progress-msg" :title="app.runtimeInstallMessage">{{ app.runtimeInstallMessage }}</span>
                  <div class="runtime-env-card__progress-actions">
                    <n-button size="tiny" tertiary @click="runtimeLogDialogVisible = true">
                      <template #icon><n-icon :component="DocumentTextOutline" /></template>
                      {{ t('settings.runtimeShowInstallLog') }}
                    </n-button>
                    <n-button size="tiny" tertiary @click="cancelRuntimeInstall">
                      {{ t('common.cancel') }}
                    </n-button>
                  </div>
                </div>
                <div v-if="card.state === 'active' && runtimeCoreUpdating" class="runtime-env-card__progress">
                  <n-spin size="small" />
                  <span class="runtime-env-card__progress-msg">{{ app.runtimeCoreUpdateMessage || t('settings.runtimeCoreUpdating') }}</span>
                </div>
              </div>
            </div>

            <p v-if="runtimeDiskMeasuring" class="setting-row__hint runtime-disk-total">
              {{ t('settings.runtimeDiskMeasuring') }}
            </p>
            <p v-else-if="runtimeDiskTotalLabel" class="setting-row__hint runtime-disk-total">
              {{ t('settings.runtimeDiskTotal', { size: runtimeDiskTotalLabel }) }}
            </p>

            <div class="runtime-mirror-row">
              <label class="text-muted text-sm">{{ t('settings.runtimeMirrorLabel') }}</label>
              <n-select v-model:value="runtimeMirror" size="small" class="runtime-mirror-row__select" :options="[
                { label: t('onboarding.runtimeMirrorAuto'), value: 'auto' },
                { label: t('onboarding.runtimeMirrorUstc'), value: 'ustc' },
                { label: t('onboarding.runtimeMirrorTsinghua'), value: 'tsinghua' },
                { label: t('onboarding.runtimeMirrorAliyun'), value: 'aliyun' },
                { label: t('onboarding.runtimeMirrorTencent'), value: 'tencent' },
                { label: t('onboarding.runtimeMirrorPypi'), value: 'pypi' },
              ]" />
            </div>
            <p class="runtime-mirror-hint">{{ t('settings.runtimeMirrorHint') }}</p>

            <n-alert v-if="app.runtimeInstallStatus === 'error'" type="error" :show-icon="true">
              <div class="runtime-install-error">
                <div class="runtime-install-error__message">{{ app.runtimeInstallMessage }}</div>
                <div class="runtime-install-error__actions">
                  <n-button
                    size="small"
                    tertiary
                    @click="runtimeLogDialogVisible = true"
                  >
                    <template #icon><n-icon :component="DocumentTextOutline" /></template>
                    {{ t('settings.runtimeShowInstallLog') }}
                  </n-button>
                  <n-button v-if="app.runtimeInfo?.logPath" size="small" tertiary @click="revealPath(app.runtimeInfo.logPath)">
                    {{ t('settings.runtimeOpenInstallLog') }}
                  </n-button>
                  <n-button size="small" secondary :disabled="runtimeBusy" @click="retryRuntimeInstall">
                    {{ t('settings.runtimeRetryInstall') }}
                  </n-button>
                </div>
              </div>
            </n-alert>

            <n-alert v-if="app.runtimeCoreUpdateStatus === 'error'" type="error" :show-icon="true">
              <div class="runtime-install-error">
                <div class="runtime-install-error__message">{{ app.runtimeCoreUpdateMessage }}</div>
                <div class="runtime-install-error__actions">
                  <n-button v-if="app.runtimeInfo?.logPath" size="small" tertiary @click="revealPath(app.runtimeInfo.logPath)">
                    {{ t('settings.runtimeOpenInstallLog') }}
                  </n-button>
                </div>
              </div>
            </n-alert>

            <div v-if="!runtimeInstalling && app.runtimeInstallLogs.length" class="runtime-log-trigger">
              <n-button size="small" tertiary @click="runtimeLogDialogVisible = true">
                <template #icon><n-icon :component="DocumentTextOutline" /></template>
                {{ t('settings.runtimeShowInstallLog') }}
              </n-button>
            </div>
          </article>
        </section>

        <!-- Paths -->
        <section v-else-if="activeSection === 'paths'" class="settings-section-panel">
        <n-card class="settings-card settings-card--feature settings-card--paths" :bordered="true" size="small">
          <template #header>
            <div class="section-title">
              <span class="section-title__icon">
                <n-icon :component="FolderOpenOutline" size="18" />
              </span>
              <span>{{ t('settings.dataDir') }}</span>
            </div>
          </template>

          <div class="path-panel">
            <div class="path-panel__intro">{{ t('settings.pathsHint') }}</div>

            <div class="path-root-block">
              <div class="path-root-row">
                <code class="path-root" :title="dataRoot || t('common.notSet')">{{ dataRoot || t('common.notSet') }}</code>
                <n-button secondary size="small" :disabled="!dataRoot" @click="revealPath(dataRoot)">
                  {{ t('common.open') }}
                </n-button>
              </div>
            </div>

            <div class="path-primary-grid">
              <div class="path-item path-item--primary">
                <div class="path-item__head">
                  <div class="path-item__head-copy">
                    <strong>{{ t('settings.modelDir') }}</strong>
                  </div>
                  <div class="path-item__actions">
                    <n-tag v-if="hasActiveModelDirUsage && !isModelDirMigrating" :bordered="false" size="small" type="warning">
                      {{ t('settings.modelDirInUse') }}
                    </n-tag>
                    <n-button
                      secondary
                      type="primary"
                      size="small"
                      :loading="isCheckingModelDir"
                      :disabled="hasActiveModelDirUsage || isCheckingModelDir"
                      @click="changeModelDir"
                    >
                      <template #icon><n-icon :component="SwapHorizontalOutline" /></template>
                      {{ t('settings.changeModelDir') }}
                    </n-button>
                  </div>
                </div>
                <code class="path-item__value" :title="modelDir || t('common.notSet')">{{ modelDir || t('common.notSet') }}</code>
              </div>

              <div class="path-item path-item--primary">
                <div class="path-item__head">
                  <div class="path-item__head-copy">
                    <strong>{{ t('settings.outputDir') }}</strong>
                  </div>
                  <div class="path-item__actions">
                    <n-button secondary type="primary" size="small" @click="changeOutputDir">
                      <template #icon><n-icon :component="FolderOpenOutline" /></template>
                      {{ t('settings.changeOutputDir') }}
                    </n-button>
                  </div>
                </div>
                <code class="path-item__value" :title="outputDir || t('common.notSet')">{{ outputDir || t('common.notSet') }}</code>
              </div>
            </div>

            <div class="path-grid">
              <div v-for="entry in dataDirEntries" :key="entry.key" class="path-subcard">
                <span>{{ t(entry.key) }}</span>
                <code class="path-field__value" :title="entry.value || entry.fallback">{{ dirName(entry.value, entry.fallback) }}</code>
              </div>
            </div>
          </div>
        </n-card>
        </section>

        <!-- Defaults & Execution -->
        <section v-else-if="activeSection === 'defaults'" class="settings-section-panel">
        <n-card class="settings-card" :bordered="true" size="small">
          <template #header>
            <div class="section-title">
              <span class="section-title__icon">
                <n-icon :component="SettingsOutline" size="18" />
              </span>
              <span>{{ t('settings.defaults') }}</span>
            </div>
          </template>

          <div class="settings-merged-layout">
            <section class="settings-group">
              <div class="setting-field">
                <label class="text-muted text-sm">{{ t('settings.defaultDevice') }}</label>
                <n-select
                  v-model:value="defaultDevice"
                  :options="deviceOptions"
                />
              </div>
            </section>

            <section class="settings-group">
              <div class="setting-field">
                <label class="text-muted text-sm">{{ t('settings.downloadSource') }}</label>
                <n-select
                  v-model:value="downloadSource"
                  :options="[
                    { label: 'ModelScope', value: 'modelscope' },
                    { label: 'Hugging Face', value: 'huggingface' },
                    { label: 'HF Mirror', value: 'hf-mirror' },
                  ]"
                />
              </div>
              <div class="setting-field">
                <label class="text-muted text-sm">{{ t('settings.downloadMethod') }}</label>
                <n-select
                  v-model:value="downloadMethod"
                  :options="downloadMethodOptions"
                />
                <p class="text-muted text-sm setting-field__hint">{{ t('settings.downloadMethodHint') }}</p>
              </div>
            </section>

            <section class="settings-group">
              <div class="setting-field">
                <label class="text-muted text-sm">{{ t('settings.proxyMode') }}</label>
                <n-select
                  v-model:value="proxyMode"
                  :options="proxyModeOptions"
                  @update:value="resetProxyTest"
                />
                <p class="text-muted text-sm setting-field__hint">{{ t('settings.proxyModeHint') }}</p>
              </div>
              <div v-if="proxyMode === 'custom'" class="proxy-custom-grid">
                <div class="setting-field">
                  <label class="text-muted text-sm">{{ t('settings.proxyUrl') }}</label>
                  <n-input
                    v-model:value="proxyUrl"
                    :placeholder="t('settings.proxyUrlPlaceholder')"
                    clearable
                    size="small"
                    @update:value="resetProxyTest"
                  />
                </div>
                <div class="setting-field">
                  <label class="text-muted text-sm">{{ t('settings.proxyBypass') }}</label>
                  <n-input
                    v-model:value="proxyBypass"
                    :placeholder="t('settings.proxyBypassPlaceholder')"
                    size="small"
                  />
                </div>
              </div>
              <div class="setting-field">
                <label class="text-muted text-sm">{{ t('settings.proxyTest') }}</label>
                <div class="proxy-test-row">
                  <n-button
                    size="small"
                    secondary
                    :loading="proxyTestLoading"
                    :disabled="proxyMode === 'custom' && !proxyUrl.trim()"
                    @click="testProxyConnection"
                  >
                    {{ t('settings.proxyTest') }}
                  </n-button>
                  <span v-if="proxyTestStatus === 'success'" class="proxy-test-info">
                    <n-icon :component="CheckmarkCircleOutline" size="14" color="var(--success)" />
                    <span class="proxy-test-info__text">{{ proxyTestMessage }}</span>
                  </span>
                  <n-tag
                    v-else-if="proxyTestStatus === 'error'"
                    size="small"
                    type="error"
                    round
                    :bordered="false"
                  >{{ t('settings.proxyTestFailed') }}</n-tag>
                  <n-tag
                    v-else-if="proxyTestStatus === 'testing'"
                    size="small"
                    :bordered="false"
                  >{{ t('settings.proxyTesting') }}</n-tag>
                </div>
                <div v-if="proxyTestStatus === 'error' && proxyTestMessage" class="proxy-test-error">
                  <div class="proxy-test-error__head">
                    <n-icon :component="AlertCircleOutline" size="14" color="var(--danger)" />
                    <span>{{ proxyTestMessage }}</span>
                  </div>
                  <p v-if="proxyTestSuggestion" class="proxy-test-error__tip">{{ proxyTestSuggestion }}</p>
                </div>
              </div>
            </section>

            <section class="settings-group settings-group--soft">
              <div class="settings-group__head">
                <span class="settings-group__icon">
                  <n-icon :component="SpeedometerOutline" size="16" />
                </span>
                <span>{{ t('settings.execution') }}</span>
              </div>
              <div class="setting-field">
                <label class="text-muted text-sm">{{ t('settings.maxConcurrentSeparations') }}</label>
                <n-input-number
                  v-model:value="maxConcurrentSeparationsInput"
                  :min="1"
                  :max="settings.MAX_CONCURRENT_SEPARATIONS"
                  :precision="0"
                  :step="1"
                  clearable
                  style="width: 100%;"
                />
                <p class="text-muted text-sm setting-field__hint">
                  {{ t('settings.maxConcurrentSeparationsHint') }}
                </p>
              </div>
            </section>

            <section class="settings-group settings-group--soft">
              <div class="settings-group__head">
                <span class="settings-group__icon">
                  <n-icon :component="TerminalOutline" size="16" />
                </span>
                <span>{{ t('settings.developerMode') }}</span>
              </div>
              <div class="setting-field setting-field--switch">
                <div class="setting-switch-row">
                  <div class="setting-switch-row__copy">
                    <label class="text-muted text-sm">{{ t('settings.developerModeTitle') }}</label>
                    <p class="text-muted text-sm setting-field__hint">
                      {{ t('settings.developerModeHint') }}
                    </p>
                  </div>
                  <n-switch v-model:value="developerMode" />
                </div>
              </div>
            </section>
          </div>
        </n-card>
        </section>
      </main>
    </div>

    <n-modal
      :show="isCheckingModelDir"
      style="width:min(420px, 88vw)"
      preset="card"
      :title="t('settings.modelDirCheckingTitle')"
      :mask-closable="false"
      :closable="false"
    >
      <div class="checking-dialog">
        <n-spin size="large" />
        <p class="checking-dialog__text">{{ t('settings.modelDirCheckingHint') }}</p>
      </div>
    </n-modal>

    <n-modal
      :show="modelDirMigrationState.status === 'confirm'"
      style="width:min(720px, 92vw)"
      preset="card"
      :title="t('settings.modelDirMigrationConfirmTitle')"
      :mask-closable="false"
      :closable="false"
      @close="closeModelDirMigrationDialog"
    >
      <div class="migration-dialog">
        <p class="migration-dialog__lead">{{ t('settings.modelDirMigrationConfirmLead') }}</p>
        <div class="migration-summary-grid">
          <div class="migration-summary-card migration-summary-card--path">
            <span>{{ t('settings.modelDirMigrationSource') }}</span>
            <code :title="modelDirMigrationState.sourceModelDir">{{ modelDirMigrationState.sourceModelDir }}</code>
          </div>
          <div class="migration-summary-card migration-summary-card--path">
            <span>{{ t('settings.modelDirMigrationTarget') }}</span>
            <code :title="modelDirMigrationState.targetModelDir">{{ modelDirMigrationState.targetModelDir }}</code>
          </div>
          <div class="migration-summary-card">
            <span>{{ t('settings.modelDirMigrationFileCount') }}</span>
            <strong>{{ modelDirMigrationState.totalFiles }}</strong>
          </div>
          <div class="migration-summary-card">
            <span>{{ t('settings.modelDirMigrationTotalSize') }}</span>
            <strong>{{ formatBytes(modelDirMigrationState.totalBytes) }}</strong>
          </div>
        </div>

        <n-alert v-if="modelDirMigrationState.preparation?.diskInsufficient" type="error" :show-icon="true" style="margin-top: 12px">
          {{ t('settings.modelDirMigrationDiskInsufficientHint', { available: formatBytes(modelDirMigrationState.preparation?.diskAvailableBytes ?? 0), needed: formatBytes(modelDirMigrationState.totalBytes) }) }}
        </n-alert>
        <n-alert v-else type="warning" :show-icon="true" style="margin-top: 12px">
          {{ t('settings.modelDirMigrationCloseWarning') }}
        </n-alert>
        <n-alert v-if="(modelDirMigrationState.preparation?.conflictCount || 0) > 0" type="info" :show-icon="true" style="margin-top: 8px">
          {{ t('settings.modelDirMigrationConflictHint', { count: modelDirMigrationState.preparation?.conflictCount || 0 }) }}
        </n-alert>
      </div>
      <template #footer>
        <div class="migration-dialog__footer">
          <n-button @click="closeModelDirMigrationDialog">{{ t('common.cancel') }}</n-button>
          <n-button type="primary" :disabled="!!modelDirMigrationState.preparation?.diskInsufficient" @click="confirmModelDirMigration">{{ t('settings.modelDirMigrationStart') }}</n-button>
        </div>
      </template>
    </n-modal>

    <n-modal
      :show="modelDirMigrationVisible"
      style="width:min(760px, 92vw)"
      preset="card"
      :title="t('settings.modelDirMigrationTitle')"
      :mask-closable="false"
      :closable="modelDirMigrationHasResult"
      @close="closeModelDirMigrationDialog"
    >
      <div class="migration-dialog">
        <div class="migration-dialog__status">
          <div>
            <strong>{{ modelDirMigrationState.message || t('settings.modelDirMigrationPreparing') }}</strong>
            <p>{{ t('settings.modelDirMigrationProgressText', { completed: modelDirMigrationState.completedFiles, total: modelDirMigrationState.totalFiles }) }}</p>
          </div>
          <n-tag :bordered="false" size="small" :type="modelDirMigrationState.status === 'failed' ? 'error' : modelDirMigrationState.status === 'success' ? 'success' : modelDirMigrationState.status === 'aborted' ? 'default' : modelDirMigrationHasConflict ? 'warning' : 'info'">
            {{ modelDirMigrationState.status }}
          </n-tag>
        </div>

        <n-progress
          type="line"
          :percentage="modelDirMigrationProgress"
          :show-indicator="true"
          :height="12"
          :border-radius="8"
          status="default"
        />

        <div class="migration-progress-meta">
          <span>{{ t('settings.modelDirMigrationByteProgress', { copied: formatBytes(modelDirMigrationState.copiedBytes), total: formatBytes(modelDirMigrationState.totalBytes) }) }}</span>
          <span>{{ t('settings.modelDirMigrationProgressText', { completed: modelDirMigrationState.completedFiles, total: modelDirMigrationState.totalFiles }) }}</span>
        </div>

        <div class="migration-summary-grid">
          <div class="migration-summary-card">
            <span>{{ t('settings.modelDirMigrationSource') }}</span>
            <code>{{ modelDirMigrationState.sourceModelDir }}</code>
          </div>
          <div class="migration-summary-card">
            <span>{{ t('settings.modelDirMigrationTarget') }}</span>
            <code>{{ modelDirMigrationState.targetModelDir }}</code>
          </div>
        </div>

        <div v-if="modelDirMigrationState.currentPath" class="migration-current-path">
          <span>{{ t('settings.modelDirMigrationCurrentFile') }}</span>
          <code>{{ modelDirMigrationState.currentPath }}</code>
        </div>

        <n-alert v-if="modelDirMigrationHasConflict && modelDirMigrationState.conflict" type="warning" :show-icon="true">
          <template #header>{{ t('settings.modelDirMigrationConflictTitle') }}</template>
          <div class="migration-conflict">
            <p>{{ t('settings.modelDirMigrationConflictPrompt') }}</p>
            <code>{{ modelDirMigrationState.conflict.destinationPath }}</code>
            <small>{{ t('settings.modelDirMigrationConflictApplyAll') }}</small>
          </div>
        </n-alert>

        <n-alert v-if="modelDirMigrationState.status === 'failed'" type="error" :show-icon="true">
          {{ modelDirMigrationState.error || t('settings.modelDirMigrationFailedHint') }}
        </n-alert>

        <n-alert v-if="modelDirMigrationState.status === 'aborted'" type="default" :show-icon="true">
          {{ t('settings.modelDirMigrationAbortedHint') }}
        </n-alert>

        <n-alert v-if="modelDirMigrationState.status === 'success' && modelDirMigrationState.cleanupFailedFiles.length" type="warning" :show-icon="true">
          {{ t('settings.modelDirMigrationCleanupFailedHint', { count: modelDirMigrationState.cleanupFailedFiles.length }) }}
        </n-alert>
      </div>
      <template #footer>
        <div class="migration-dialog__footer">
          <template v-if="modelDirMigrationHasConflict">
            <n-button :loading="modelDirMigrationState.resolvingConflict" @click="resolveModelDirConflict('skip')">
              {{ t('settings.modelDirMigrationSkip') }}
            </n-button>
            <n-button type="warning" :loading="modelDirMigrationState.resolvingConflict" @click="resolveModelDirConflict('overwrite')">
              {{ t('settings.modelDirMigrationOverwrite') }}
            </n-button>
            <n-button type="error" secondary :loading="modelDirMigrationState.resolvingConflict" @click="resolveModelDirConflict('abort')">
              {{ t('settings.modelDirMigrationAbort') }}
            </n-button>
          </template>
          <template v-else-if="modelDirMigrationHasResult">
            <n-button type="primary" @click="closeModelDirMigrationDialog">{{ t('common.confirm') }}</n-button>
          </template>
          <template v-else>
            <n-button disabled>{{ t('settings.modelDirMigrationCloseBlocked') }}</n-button>
          </template>
        </div>
      </template>
    </n-modal>

    <n-modal
      v-model:show="runtimeLogDialogVisible"
      style="width:min(760px, 92vw)"
      preset="card"
      :title="t('settings.runtimeInstallLogTitle')"
    >
      <div class="runtime-log-dialog">
        <div v-if="runtimeInstalling" class="runtime-log-dialog__status">
          <n-spin size="small" />
          <span>{{ app.runtimeInstallMessage }}</span>
        </div>
        <n-alert v-else-if="app.runtimeInstallStatus === 'error'" type="error" :show-icon="true" style="margin-bottom: 8px">
          {{ app.runtimeInstallMessage }}
        </n-alert>
        <n-alert v-else-if="app.runtimeInstallStatus === 'success'" type="success" :show-icon="true" style="margin-bottom: 8px">
          {{ t('settings.runtimeInstallLogSuccess') }}
        </n-alert>
        <pre v-if="app.runtimeInstallLogs.length" ref="runtimeLogPre" class="runtime-log-dialog__pre">{{ app.runtimeInstallLogs.join('\n') }}</pre>
        <div v-if="app.runtimeInfo?.logPath" class="runtime-log-dialog__path">
          <code>{{ app.runtimeInfo.logPath }}</code>
          <n-button size="tiny" tertiary @click="revealPath(app.runtimeInfo.logPath)">
            {{ t('settings.runtimeOpenInstallLog') }}
          </n-button>
        </div>
        <p v-if="!app.runtimeInstallLogs.length && !app.runtimeInfo?.logPath" class="runtime-log-dialog__empty">
          {{ t('settings.runtimeInstallLogEmpty') }}
        </p>
      </div>
    </n-modal>
  </div>
</template>

<style scoped>
.page--settings {
  max-width: var(--page-max-width-reading);
}

.page-header-compact {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.page-header-compact__main {
  min-width: 0;
}

.settings-shell {
  display: grid;
  grid-template-columns: minmax(200px, 220px) minmax(0, 1fr);
  gap: 20px;
  align-items: start;
}

.settings-sidebar {
  position: sticky;
  top: 18px;
  display: grid;
  gap: 4px;
  padding: 8px;
  border: 1px solid color-mix(in srgb, var(--outline) 50%, transparent);
  border-radius: 16px;
  background: color-mix(in srgb, var(--surface-1) 78%, transparent);
}

.settings-nav-item {
  width: 100%;
  display: grid;
  grid-template-columns: 32px minmax(0, 1fr);
  gap: 10px;
  align-items: center;
  padding: 9px 10px;
  border: 1px solid transparent;
  border-radius: 12px;
  color: var(--on-surface-muted);
  background: transparent;
  cursor: pointer;
  text-align: left;
  transition: color 160ms ease, background 180ms ease, border-color 180ms ease;
}

.settings-nav-item:hover {
  color: var(--on-surface);
  background: color-mix(in srgb, var(--surface-3) 50%, transparent);
}

.settings-nav-item.active {
  color: color-mix(in srgb, var(--primary-strong) 86%, var(--on-surface));
  border-color: color-mix(in srgb, var(--primary-border) 38%, transparent);
  background: color-mix(in srgb, var(--primary-soft) 28%, var(--surface-2));
}

.settings-nav-item__icon {
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  background: color-mix(in srgb, var(--surface-2) 65%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 32%, transparent);
}

.settings-nav-item.active .settings-nav-item__icon {
  background: color-mix(in srgb, var(--primary-soft) 48%, var(--surface-2));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 42%, transparent);
}

.settings-nav-item__copy {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.settings-nav-item__copy strong {
  color: inherit;
  font-size: 13px;
  font-weight: 700;
  line-height: 1.3;
}

.settings-nav-item__copy small {
  overflow: hidden;
  color: color-mix(in srgb, currentColor 70%, var(--on-surface-muted));
  font-size: 11px;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.settings-content {
  min-width: 0;
  display: grid;
  gap: 16px;
}

.settings-section-panel {
  display: block;
  min-width: 0;
}

.settings-section-panel > :deep(.n-card) {
  width: 100%;
}

.about-panel {
  display: grid;
  gap: 16px;
}

.about-hero,
.about-info-card {
  border: 1px solid color-mix(in srgb, var(--outline) 48%, transparent);
  background: color-mix(in srgb, var(--surface-1) 82%, transparent);
  box-shadow: inset 0 1px 0 color-mix(in srgb, white 8%, transparent);
}

.about-hero {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 20px 22px;
  border-radius: 20px;
}

.about-hero__main {
  display: flex;
  align-items: center;
  gap: 18px;
  min-width: 0;
}

.about-logo-wrap {
  flex: 0 0 auto;
  width: 80px;
  height: 80px;
  display: grid;
  place-items: center;
  border-radius: 20px;
  background: color-mix(in srgb, var(--surface-2) 70%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 40%, transparent);
}

.about-logo {
  width: 62px;
  height: 62px;
  object-fit: contain;
}

.about-hero__copy {
  min-width: 0;
  flex: 1 1 auto;
}

.about-eyebrow {
  display: inline-flex;
  align-items: center;
  min-height: 22px;
  padding: 0 9px;
  border-radius: 999px;
  color: color-mix(in srgb, var(--primary-strong) 88%, var(--on-surface));
  background: color-mix(in srgb, var(--primary-soft) 42%, transparent);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.02em;
  white-space: nowrap;
}

.about-hero__title {
  margin: 8px 0 4px;
  color: var(--on-surface);
  font-size: clamp(22px, 2.6vw, 28px);
  font-weight: 700;
  letter-spacing: -0.03em;
  line-height: 1.15;
  white-space: nowrap;
}

.about-hero__tagline {
  margin: 0;
  color: var(--on-surface-muted);
  font-size: 13px;
  line-height: 1.6;
}

.about-hero__tech {
  margin: 8px 0 0;
  color: color-mix(in srgb, var(--on-surface-muted) 92%, var(--on-surface));
  font-size: 12px;
  line-height: 1.65;
}

.about-stats {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  width: 100%;
  min-width: 0;
}

.about-stat {
  display: grid;
  gap: 1px;
  min-width: 0;
  padding: 10px 12px;
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface-2) 55%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 32%, transparent);
}

.about-stat__label {
  overflow: hidden;
  color: var(--on-surface-muted);
  font-size: clamp(10px, 1.1vw, 11px);
  font-weight: 600;
  line-height: 1.3;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.about-stat__value {
  overflow: hidden;
  color: var(--on-surface);
  font-size: clamp(14px, 1.6vw, 16px);
  font-weight: 700;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.about-stat__meta {
  overflow: hidden;
  color: color-mix(in srgb, var(--on-surface-muted) 88%, var(--on-surface));
  font-size: clamp(9px, 1vw, 10px);
  font-weight: 600;
  line-height: 1.3;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.about-build-card {
  display: grid;
  gap: 10px;
  padding: 16px 18px;
  border: 1px solid color-mix(in srgb, var(--outline) 36%, transparent);
  border-radius: 16px;
  background: color-mix(in srgb, var(--surface-2) 38%, transparent);
}

.about-build-card--official {
  border-color: color-mix(in srgb, #22c55e 34%, var(--outline));
  background: color-mix(in srgb, #22c55e 8%, var(--surface-2));
}

.about-build-card--development {
  border-color: color-mix(in srgb, #f2c94c 38%, var(--outline));
  background: color-mix(in srgb, #f2c94c 8%, var(--surface-2));
}

.about-build-card__head,
.about-build-fingerprint {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  min-width: 0;
}

.about-build-card__head span,
.about-build-fingerprint span {
  color: var(--on-surface-muted);
  font-size: 12px;
  font-weight: 700;
}

.about-build-card__head strong {
  color: var(--on-surface);
  font-size: 14px;
  font-weight: 800;
}

.about-build-card p {
  margin: 0;
  color: var(--on-surface-muted);
  font-size: 13px;
  line-height: 1.6;
}

.about-build-fingerprint code {
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--on-surface);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  font-weight: 700;
  text-align: right;
}

.about-info-card--update {
  display: grid;
  gap: 12px;
}

.update-panel {
  display: grid;
  gap: 12px;
}

.update-panel__overview {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 16px;
  border: 1px solid color-mix(in srgb, var(--primary) 20%, var(--outline));
  border-radius: 14px;
  background: color-mix(in srgb, var(--primary-softer) 70%, var(--surface));
}

.update-panel__headline {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  min-width: 0;
}

.update-panel__status-dot {
  flex: 0 0 auto;
  width: 9px;
  height: 9px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--on-surface-muted);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--on-surface-muted) 14%, transparent);
}

.update-panel__status-dot--success {
  background: var(--success);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--success) 15%, transparent);
}

.update-panel__status-dot--warning {
  background: var(--warning);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--warning) 15%, transparent);
}

.update-panel__status-dot--error {
  background: var(--danger);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--danger) 15%, transparent);
}

.update-panel__headline strong {
  display: block;
  color: var(--on-surface);
  font-size: 15px;
  font-weight: 800;
}

.update-panel__headline p {
  margin: 4px 0 0;
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.5;
}

.update-panel__tags {
  flex: 0 0 auto;
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
}

.update-panel__version-flow {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: stretch;
  gap: 10px;
}

.update-panel__version-card {
  display: grid;
  gap: 4px;
  min-width: 0;
  padding: 12px 14px;
  border: 1px solid var(--outline);
  border-radius: 12px;
  background: var(--surface-2);
}

.update-panel__version-card span {
  color: var(--on-surface-muted);
  font-size: 11px;
  font-weight: 700;
}

.update-panel__version-card strong {
  overflow: hidden;
  color: var(--on-surface);
  font-size: 17px;
  font-weight: 800;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.update-panel__version-card--target {
  border-color: color-mix(in srgb, var(--success) 35%, var(--outline));
  background: color-mix(in srgb, var(--success) 8%, var(--surface-2));
}

.update-panel__version-arrow {
  align-self: center;
  color: var(--on-surface-muted);
  font-size: 20px;
  font-weight: 700;
}

.update-panel__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.5;
}

.update-panel__channel {
  display: grid;
  grid-template-columns: auto minmax(180px, 1fr);
  align-items: center;
  gap: 12px;
  color: var(--on-surface-muted);
  font-size: 12px;
  font-weight: 700;
}

.update-panel__channel .n-select {
  width: min(100%, 320px);
}

.update-panel__release-notes {
  display: grid;
  gap: 6px;
}

.update-panel__release-notes > span {
  color: var(--on-surface-muted);
  font-size: 12px;
  font-weight: 700;
}

.update-panel__notes {
  margin: 0;
  padding: 12px 14px;
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface-2) 42%, transparent);
  color: var(--on-surface);
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
}

.update-panel__notes--muted {
  color: var(--on-surface-muted);
}

.update-panel__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

@media (max-width: 640px) {
  .update-panel__overview {
    flex-direction: column;
  }

  .update-panel__tags {
    justify-content: flex-start;
  }

  .update-panel__version-flow {
    grid-template-columns: minmax(0, 1fr);
  }

  .update-panel__version-arrow {
    justify-self: center;
    transform: rotate(90deg);
  }

  .update-panel__channel {
    grid-template-columns: minmax(0, 1fr);
  }

  .update-panel__channel .n-select {
    width: 100%;
  }
}

.about-detail-grid {
  display: grid;
  grid-template-columns: minmax(220px, 280px) minmax(0, 1fr);
  gap: 16px;
  align-items: stretch;
}

.about-info-card {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
  padding: 18px 20px;
  border-radius: 18px;
}

.about-info-card__body {
  display: grid;
  gap: 10px;
  flex: 1;
}

.about-info-card__body p {
  margin: 0;
  color: var(--on-surface-muted);
  font-size: 13px;
  line-height: 1.75;
}

.section-title--plain {
  justify-content: flex-start;
}

.license-stack {
  display: grid;
  gap: 8px;
  flex: 1;
  align-content: start;
}

.license-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
  padding: 11px 12px;
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface-2) 48%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 28%, transparent);
}

.license-row span {
  color: var(--on-surface-muted);
  font-size: 12px;
  font-weight: 600;
}

.license-row strong {
  color: var(--on-surface);
  font-size: 13px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.about-link-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.about-link-item {
  display: grid;
  grid-template-columns: 32px minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  min-width: 0;
  min-height: 52px;
  padding: 10px 12px;
  border: 1px solid color-mix(in srgb, var(--outline) 36%, transparent);
  border-radius: 14px;
  color: var(--on-surface);
  background: color-mix(in srgb, var(--surface-2) 32%, transparent);
  cursor: pointer;
  text-align: left;
  transition: transform 160ms ease, border-color 180ms ease, background 180ms ease;
}

.about-link-item:hover {
  transform: translateY(-1px);
  border-color: color-mix(in srgb, var(--primary-border) 50%, var(--outline));
  background: color-mix(in srgb, var(--primary-soft) 16%, var(--surface-2));
}

.about-link-item__icon {
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  color: var(--primary-strong);
  background: color-mix(in srgb, var(--primary-soft) 36%, transparent);
}

.about-link-item__label {
  overflow: hidden;
  font-size: 13px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.about-link-item__open {
  color: var(--on-surface-muted);
  opacity: 0.7;
}

.runtime-hero__icon {
  flex: 0 0 auto;
}

.runtime-hero__desc {
  margin: 6px 0 0;
  color: var(--on-surface-muted);
  font-size: 13px;
  line-height: 1.6;
}

.runtime-hero__desc--error {
  color: var(--danger, #dc2626);
}

.runtime-detail-grid {
  align-items: start;
}

.runtime-installed-list {
  display: grid;
  gap: 8px;
}

.runtime-installed-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface-2) 48%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 28%, transparent);
  color: var(--on-surface-muted);
  font-size: 12px;
}

.runtime-installed-item strong {
  color: var(--on-surface);
  font-size: 12px;
  font-weight: 700;
}

.runtime-section {
  display: grid;
  gap: 16px;
}

.runtime-envs-card {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.runtime-envs-list {
  display: grid;
  gap: 10px;
}

.runtime-env-card {
  display: grid;
  min-width: 0;
  gap: 8px;
  padding: 14px 16px;
  border-radius: 14px;
  border: 1px solid color-mix(in srgb, var(--outline) 42%, transparent);
  background: color-mix(in srgb, var(--surface-2) 32%, transparent);
  transition: border-color 180ms ease, background 180ms ease;
}

.runtime-env-card--active {
  border-color: color-mix(in srgb, var(--primary-border) 52%, var(--outline));
  background: color-mix(in srgb, var(--primary-soft) 12%, var(--surface-2) 28%);
}

.runtime-env-card--muted {
  background: color-mix(in srgb, var(--surface-2) 18%, transparent);
  border-style: dashed;
}

.runtime-env-card__desc {
  margin: 0;
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.55;
}

.runtime-env-card__locked {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
  padding: 8px 10px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--surface-2) 58%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 26%, transparent);
  color: var(--on-surface-muted);
  font-size: 12px;
}

.runtime-env-card__progress {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  min-width: 0;
  margin-top: 2px;
  padding: 8px 10px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--surface-2) 52%, transparent);
  color: var(--on-surface-muted);
  font-size: 12px;
}

.runtime-env-card__progress-msg {
  min-width: 0;
  overflow: hidden;
  max-width: 100%;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.runtime-env-card__progress-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  min-width: 0;
}

.runtime-env-card__progress-actions :deep(.n-button) {
  flex: 0 0 auto;
}

.runtime-disk-total {
  margin: 0;
}

.runtime-mirror-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding-top: 2px;
}

.runtime-mirror-row__select {
  width: 220px;
}

.runtime-mirror-hint {
  margin: -4px 0 0;
  color: var(--on-surface-muted);
  font-size: 11px;
  line-height: 1.5;
}

.runtime-env-card__head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.runtime-env-card__backend {
  color: var(--on-surface);
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.01em;
}

.runtime-env-card__meta {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  color: var(--on-surface-muted);
  font-size: 12px;
}

.runtime-env-card__accel {
  color: var(--on-surface-muted);
}

.runtime-env-card__accel--ok {
  color: var(--success, #22c55e);
}

.runtime-env-card__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding-top: 4px;
}

.runtime-install-error {
  display: grid;
  gap: 8px;
  min-width: 0;
}

.runtime-install-error__message {
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.runtime-install-error__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.settings-card {
  border-color: color-mix(in srgb, var(--outline) 58%, transparent) !important;
  background:
    linear-gradient(180deg, rgba(255,255,255,0.025), transparent 42%),
    color-mix(in srgb, var(--surface-1) 72%, transparent) !important;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
}

.settings-card :deep(.n-card__header) {
  padding: 18px 20px 12px;
  border-bottom: 1px solid color-mix(in srgb, var(--outline) 42%, transparent);
}

.settings-card :deep(.n-card__content) {
  padding: 4px 20px 18px;
}

.settings-card--compact :deep(.n-card__content) {
  padding-bottom: 20px;
}

.settings-card--feature {
  position: relative;
}

.settings-card--feature::after {
  display: none;
}

.section-title {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 10px;
  font-size: 15px;
  font-weight: 600;
}

.section-title__icon {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  color: color-mix(in srgb, var(--primary-strong) 74%, var(--on-surface-muted));
  background: color-mix(in srgb, var(--primary-soft) 34%, var(--surface-2));
  box-shadow:
    inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 36%, transparent);
}

.appearance-list {
  display: grid;
}

.appearance-hint {
  margin: 0 0 4px;
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.7;
}

.setting-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 16px;
  padding: 13px 0;
}

.setting-row + .setting-row {
  border-top: 1px solid color-mix(in srgb, var(--outline) 50%, transparent);
}

.setting-row__label {
  display: grid;
  gap: 4px;
  min-width: 0;
}

.setting-row__title {
  color: var(--on-surface);
  font-size: 13px;
  font-weight: 600;
  line-height: 1.4;
}

.setting-row__hint {
  margin: 0;
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.6;
}

.setting-row__control {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  min-width: 0;
}

.accent-dots {
  display: flex;
  align-items: center;
  gap: 8px;
}

.accent-dot {
  width: 26px;
  height: 26px;
  padding: 0;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, var(--outline) 72%, transparent);
  background: transparent;
  cursor: pointer;
  overflow: hidden;
  transition: transform 160ms ease, box-shadow 180ms ease, border-color 180ms ease;
}

.accent-dot__fill {
  display: block;
  width: 100%;
  height: 100%;
  border-radius: inherit;
}

.accent-dot:hover {
  transform: translateY(-2px);
  border-color: color-mix(in srgb, var(--primary-border) 70%, var(--outline));
}

.accent-dot.active {
  border-color: transparent;
  box-shadow:
    0 0 0 2px var(--surface-1),
    0 0 0 4px var(--primary);
}

.accent-current {
  margin-left: auto;
  color: var(--on-surface-muted);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.2;
}

.language-select-wrap {
  width: 100%;
  max-width: 240px;
}

.scale-reset {
  justify-self: flex-start;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--primary-strong);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: color 150ms ease, opacity 150ms ease;
}

.scale-reset:hover:not(:disabled) {
  color: color-mix(in srgb, var(--primary-strong) 80%, white 20%);
}

.scale-reset:disabled {
  color: var(--on-surface-muted);
  opacity: 0.5;
  cursor: not-allowed;
}

.scale-control {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 280px;
}

.scale-control :deep(.n-slider) {
  flex: 1;
}

.scale-control :deep(.n-slider-mark) {
  font-size: 11px;
}

.scale-value {
  flex: 0 0 auto;
  color: var(--primary-strong);
  font-size: 12px;
  font-weight: 700;
  line-height: 1.2;
}

.setting-field {
  display: grid;
  gap: 10px;
}

.setting-field__hint {
  margin: 0;
  line-height: 1.6;
}

.proxy-test-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.proxy-custom-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

@media (max-width: 640px) {
  .proxy-custom-grid {
    grid-template-columns: 1fr;
  }
}

.proxy-test-info {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  max-width: 100%;
}

.proxy-test-info__text {
  font-size: 12px;
  color: var(--on-surface);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.proxy-test-error {
  margin-top: 8px;
  padding: 10px 12px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--danger) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--danger) 28%, transparent);
}

.proxy-test-error__head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 500;
  color: var(--danger);
  word-break: break-all;
}

.proxy-test-error__tip {
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.55;
  color: var(--on-surface-muted);
}

.runtime-log-trigger {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.runtime-log-trigger__status {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--on-surface-muted);
  font-size: 12px;
}

.runtime-log-dialog {
  display: grid;
  gap: 8px;
}

.runtime-log-dialog__status {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 0;
  color: var(--on-surface-muted);
  font-size: 13px;
}

.runtime-log-dialog__pre {
  max-height: 420px;
  overflow: auto;
  margin: 0;
  padding: 12px 14px;
  border-radius: 10px;
  background: var(--surface-2);
  border: 1px solid color-mix(in srgb, var(--outline) 40%, transparent);
  color: var(--on-surface-muted);
  font: 11px/1.55 "JetBrains Mono", "Cascadia Code", Consolas, ui-monospace, monospace;
  white-space: pre-wrap;
  word-break: break-word;
}

.runtime-log-dialog__empty {
  margin: 0;
  padding: 24px 0;
  text-align: center;
  color: var(--on-surface-muted);
  font-size: 13px;
}

.runtime-stack {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.runtime-stack__section {
  width: 100%;
}

.setting-field--switch {
  gap: 0;
}

.setting-switch-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 18px;
}

.setting-switch-row__copy {
  display: grid;
  gap: 6px;
  min-width: 0;
}

.settings-merged-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(320px, 0.92fr);
  gap: 14px;
  align-items: start;
}

.settings-group {
  display: grid;
  gap: 10px;
}

.settings-group:nth-child(1) {
  grid-column: 1;
  grid-row: 1;
}

.settings-group:nth-child(2) {
  grid-column: 1;
  grid-row: 2;
}

.settings-group:nth-child(3) {
  grid-column: 2;
  grid-row: 1 / span 2;
}

.settings-group--soft {
  padding: 14px 0 0;
  border-top: 1px solid color-mix(in srgb, var(--outline) 42%, transparent);
  border-radius: 0;
  background: transparent;
  align-self: stretch;
  align-content: start;
}

.settings-group__head {
  display: flex;
  align-items: center;
  gap: 8px;
  color: color-mix(in srgb, var(--on-surface-muted) 88%, var(--on-surface) 12%);
  font-size: 12px;
  font-weight: 600;
}

.settings-group__icon {
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  border-radius: 8px;
  color: color-mix(in srgb, var(--primary-strong) 78%, var(--on-surface-muted));
  background: color-mix(in srgb, var(--primary-soft) 36%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 36%, transparent);
}

.path-panel {
  position: relative;
  z-index: 1;
  display: grid;
  gap: 14px;
}

.path-panel__intro {
  margin: 0;
  color: var(--on-surface-muted);
  font-size: 13px;
  line-height: 1.6;
}

.path-root-block {
  display: grid;
  gap: 8px;
}

.path-root-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
}

.path-root {
  display: flex;
  align-items: center;
  min-height: 40px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 0 14px;
  border: 1px solid color-mix(in srgb, var(--outline) 56%, transparent);
  border-radius: 13px;
  color: color-mix(in srgb, var(--on-surface) 92%, black 8%);
  background: color-mix(in srgb, var(--surface) 26%, transparent);
  font-family: "JetBrains Mono", "Cascadia Code", Consolas, "MiSans", "PingFang SC", "Microsoft YaHei", ui-monospace, monospace;
  font-size: 13px;
  line-height: 1.45;
  letter-spacing: 0.01em;
}

.path-grid {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin-top: 2px;
  padding-top: 12px;
  border-top: 1px solid color-mix(in srgb, var(--outline) 48%, transparent);
}

.path-primary-grid {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr));
}

.path-item {
  display: grid;
  gap: 12px;
  padding: 14px;
  border-radius: 16px;
  border: 1px solid color-mix(in srgb, var(--outline) 56%, transparent);
  background: color-mix(in srgb, var(--surface) 24%, transparent);
}

.path-item--primary {
  position: relative;
  overflow: hidden;
  border-color: color-mix(in srgb, var(--primary-border) 34%, var(--outline));
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--primary-soft) 10%, transparent), transparent 58%),
    color-mix(in srgb, var(--surface) 26%, transparent);
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,0.035);
}

.path-item--primary::before {
  display: none;
}

.path-item__head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.path-item__head-copy {
  min-width: min(100%, 128px);
  flex: 1 1 128px;
  display: grid;
  gap: 4px;
}

.path-item__head-copy strong {
  font-size: 13px;
  overflow-wrap: normal;
  word-break: normal;
}

.path-item__actions {
  min-width: 0;
  flex: 0 1 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.path-item__actions :deep(.n-button) {
  max-width: 100%;
}

.path-item__value {
  display: block;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 11px 12px;
  border-radius: 12px;
  border: 1px solid color-mix(in srgb, var(--outline) 62%, transparent);
  background: color-mix(in srgb, var(--surface-2) 52%, var(--surface-1));
  color: color-mix(in srgb, var(--on-surface) 88%, var(--on-surface-muted));
  font-family: "JetBrains Mono", "Cascadia Code", Consolas, "MiSans", "PingFang SC", "Microsoft YaHei", ui-monospace, monospace;
  font-size: 12px;
}

.path-subcard {
  display: grid;
  gap: 4px;
  padding: 8px 10px;
  border-radius: 10px;
  border: 1px solid color-mix(in srgb, var(--outline) 34%, transparent);
  background: color-mix(in srgb, var(--surface-2) 18%, transparent);
}

.path-grid span {
  color: color-mix(in srgb, var(--on-surface-muted) 94%, var(--on-surface) 6%);
  font-size: 11px;
  font-weight: 600;
}

.path-field__value {
  display: block;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 6px 0 0;
  border: 0;
  border-radius: 0;
  color: color-mix(in srgb, var(--on-surface) 82%, var(--on-surface-muted));
  background: transparent;
  font-family: "JetBrains Mono", "Cascadia Code", Consolas, "MiSans", "PingFang SC", "Microsoft YaHei", ui-monospace, monospace;
  font-size: 12px;
  line-height: 1.45;
  letter-spacing: 0.01em;
}

.theme-switcher {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 4px;
  margin: 0;
  padding: 4px;
  border: 1px solid color-mix(in srgb, var(--outline) 56%, transparent);
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface) 34%, transparent);
}

.theme-switcher button {
  min-width: 0;
  border: 0;
  border-radius: 9px;
  padding: 8px 8px;
  color: var(--on-surface-muted);
  background: transparent;
  cursor: pointer;
  transition: 150ms ease;
}

.theme-switcher button:hover {
  color: var(--on-surface);
  background: color-mix(in srgb, var(--surface-3) 76%, transparent);
}

.theme-switcher button.active {
  color: color-mix(in srgb, var(--primary-strong) 86%, var(--on-surface));
  background: color-mix(in srgb, var(--primary-soft) 34%, var(--surface-3));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--primary-border) 42%, transparent);
  font-weight: 600;
}

.checking-dialog {
  display: grid;
  justify-items: center;
  gap: 14px;
  padding: 10px 0 4px;
}

.checking-dialog__text {
  margin: 0;
  color: var(--on-surface-muted);
  font-size: 13px;
  line-height: 1.7;
  text-align: center;
}

.migration-dialog {
  display: grid;
  gap: 14px;
}

.migration-dialog__lead {
  margin: 0;
  color: var(--on-surface-muted);
  line-height: 1.7;
}

.migration-dialog__status {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.migration-dialog__status p {
  margin: 6px 0 0;
  color: var(--on-surface-muted);
  font-size: 12px;
}

.migration-dialog__footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}

.migration-summary-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.migration-summary-card--path {
  grid-column: 1 / -1;
}

.migration-summary-card {
  display: grid;
  gap: 6px;
  padding: 12px;
  border-radius: 12px;
  border: 1px solid color-mix(in srgb, var(--outline) 82%, transparent);
  background: color-mix(in srgb, var(--surface-1) 96%, transparent);
}

.migration-summary-card span {
  color: var(--on-surface-muted);
  font-size: 12px;
}

.migration-summary-card strong,
.migration-summary-card code {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.migration-summary-card code,
.migration-current-path code,
.migration-conflict code {
  padding: 8px 10px;
  border-radius: 10px;
  border: 1px solid color-mix(in srgb, var(--outline) 72%, transparent);
  background: color-mix(in srgb, var(--surface-2) 76%, transparent);
  color: color-mix(in srgb, var(--on-surface) 86%, var(--on-surface-muted));
  font-family: "JetBrains Mono", "Cascadia Code", Consolas, "MiSans", "PingFang SC", "Microsoft YaHei", ui-monospace, monospace;
  font-size: 12px;
}

.migration-progress-meta {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  color: var(--on-surface-muted);
  font-size: 12px;
}

.migration-current-path {
  display: grid;
  gap: 6px;
}

.migration-current-path span {
  color: var(--on-surface-muted);
  font-size: 12px;
}

.migration-conflict {
  display: grid;
  gap: 8px;
}

.migration-conflict p,
.migration-conflict small {
  margin: 0;
}

.migration-conflict small {
  color: var(--on-surface-muted);
}

@media (max-width: 1200px) {
  .about-detail-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .about-link-list {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .path-root-row {
    grid-template-columns: minmax(0, 1fr);
  }

  .settings-merged-layout {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .settings-group:nth-child(1),
  .settings-group:nth-child(2),
  .settings-group:nth-child(3) {
    grid-column: auto;
    grid-row: auto;
  }

  .settings-group--soft {
    grid-column: 1 / -1;
  }

  .path-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .path-primary-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .migration-summary-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 840px) {
  .settings-shell {
    grid-template-columns: minmax(0, 1fr);
  }

  .settings-sidebar {
    position: static;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .about-hero__main {
    flex-direction: row;
    align-items: center;
  }

  .about-stats {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .settings-merged-layout {
    grid-template-columns: minmax(0, 1fr);
  }

  .settings-grid__top-item {
    display: block;
  }

  .settings-group--soft {
    grid-column: auto;
  }

  .path-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 640px) {
  .settings-sidebar,
  .about-link-list {
    grid-template-columns: minmax(0, 1fr);
  }

  .about-hero {
    padding: 16px;
    border-radius: 16px;
  }

  .about-hero__main {
    flex-direction: column;
    align-items: flex-start;
  }

  .about-stats {
    grid-template-columns: minmax(0, 1fr);
  }

  .about-logo-wrap {
    width: 72px;
    height: 72px;
    border-radius: 18px;
  }

  .about-logo {
    width: 56px;
    height: 56px;
  }

  .about-hero__title {
    font-size: 24px;
  }

  .page-header-compact {
    flex-direction: column;
  }


  .setting-row {
    grid-template-columns: 1fr;
  }

  .setting-row__control {
    justify-content: flex-start;
  }

  .language-select-wrap {
    max-width: none;
  }

  .scale-control {
    width: 100%;
    min-width: 0;
  }

  .runtime-env-card__progress {
    grid-template-columns: auto minmax(0, 1fr);
  }

  .runtime-env-card__progress-actions {
    grid-column: 1 / -1;
    justify-content: flex-start;
    flex-wrap: wrap;
  }

  .path-item__head,
  .migration-dialog__status {
    flex-direction: column;
  }

  .path-item__actions {
    justify-content: flex-start;
  }

  .migration-progress-meta {
    flex-direction: column;
  }
}
</style>
