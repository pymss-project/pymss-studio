<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { useRouter } from 'vue-router'
import { open } from '@tauri-apps/plugin-shell'
import { ArrowForwardOutline, CheckmarkOutline, ColorPaletteOutline, FolderOpenOutline, HardwareChipOutline, LogoGithub, MusicalNotesOutline, OpenOutline, RefreshOutline } from '@vicons/ionicons5'
import AppBrandMark from '@/components/AppBrandMark.vue'
import { SYSTEM_LOCALE, setLocale, type LocaleSetting } from '@/i18n'
import { useSettingsStore } from '@/stores/settings'
import { useAppStore, type RuntimeBackend } from '@/stores/app'
import { formatBytes } from '@/utils/format'
import { isTauriRuntime } from '@/utils/appStore'
import { runtimeErrorSummary } from '@/utils/runtimeErrors'
import {
  detectRuntimePlatform,
  preferredInstalledRuntimeBackend,
  recommendedRuntimeBackend,
  runtimeEnvironmentForBackend,
  runtimeBackendLabel as runtimeBackendName,
  runtimeSizeHint,
  runtimeGpuReportForEnvironment,
  runtimeGpuStatus,
} from '@/utils/runtime'
import {
  applyTheme,
  getThemeAccentPreview,
  resolvedIsDark,
  runRippleViewTransition,
  THEME_ACCENTS,
  type ThemeAccent,
  type ThemeMode,
} from '@/utils/theme'

const emit = defineEmits<{
  completed: []
}>()

const { t } = useI18n()
const message = useMessage()
const router = useRouter()
const settings = useSettingsStore()
const app = useAppStore()
const {
  locale,
  themeMode,
  themeAccent,
  modelDir,
  outputDir,
  modelDirMigrationState,
  isModelDirMigrating,
} = storeToRefs(settings)

const step = ref(0)
const projectRepositoryUrl = 'https://github.com/pymss-project/pymss-studio'
const checkingModelDir = ref(false)
const overlayRef = ref<HTMLElement | null>(null)
const runtimeChecking = ref(false)
const runtimeActionError = ref('')
const finishing = ref(false)

const runtimePlatform = computed(() => detectRuntimePlatform(app.runtimeInfo))
const selectedBackend = ref<RuntimeBackend>(runtimePlatform.value.isMac ? 'mlx' : 'cpu')
const waveform = [16, 24, 38, 30, 19, 44, 60, 36, 22, 48, 72, 52, 34, 62, 86, 54, 28, 46, 68, 42, 24, 56, 78, 48, 32, 64, 90, 58, 36, 50, 72, 44, 26, 38, 60, 32]

// An environment counts as installed even when it is not the active one, so this reads the
// installed list rather than app.runtimeReadyForBackend() (which only answers for the active backend).
function backendInstalled(backend: RuntimeBackend) {
  const entry = runtimeEnvironmentForBackend(app.runtimeInfo, backend)
  return Boolean(entry && entry.health !== 'broken')
}

const runtimeBackendChoices = computed(() => {
  const { isMac } = runtimePlatform.value
  const backend: RuntimeBackend = isMac ? 'mlx' : 'cpu'
  const items: Array<{ value: RuntimeBackend; label: string; hint: string }> = [
    { value: backend, label: runtimeBackendName(backend), hint: t(isMac ? 'onboarding.runtimeMlx' : 'onboarding.runtimeCpu') },
  ]
  if (!isMac) {
    items.push(
      { value: 'cuda', label: runtimeBackendName('cuda'), hint: t('onboarding.runtimeCuda') },
      { value: 'rocm', label: runtimeBackendName('rocm'), hint: t(app.runtimeInfo?.platform === 'linux' ? 'onboarding.runtimeRocmLinux' : 'onboarding.runtimeRocm') },
    )
  }
  return items.map((item) => ({
    ...item,
    installed: backendInstalled(item.value),
    recommended: runtimeRecommendedBackend.value === item.value,
    size: runtimeSizeHint(item.value),
  }))
})

const runtimeRecommendedBackend = computed(() => runtimePlatform.value.isMac ? 'mlx' : recommendedRuntimeBackend(app.runtimeInfo))
const preferredRuntimeBackend = computed(() => runtimePlatform.value.isMac ? 'mlx' : preferredInstalledRuntimeBackend(app.runtimeInfo) || runtimeRecommendedBackend.value)

// runtimeInfo arrives asynchronously, so preselect once it can actually answer — but never
// override a choice the user has already made.
const backendTouched = ref(false)
watch(preferredRuntimeBackend, (backend) => {
  if (!backend || backendTouched.value) return
  if (runtimeBackendChoices.value.some((choice) => choice.value === backend)) {
    selectedBackend.value = backend
  }
}, { immediate: true })

const selectedBackendInstalled = computed(() => backendInstalled(selectedBackend.value))
const selectedRuntime = computed(() => runtimeEnvironmentForBackend(app.runtimeInfo, selectedBackend.value))
const selectedRuntimeBroken = computed(() => selectedRuntime.value?.health === 'broken')
const selectedBackendReady = computed(() => !selectedRuntimeBroken.value && !(selectedBackend.value === 'rocm' && app.runtimeInfo?.seedPreparationRequired) && !(app.runtimeInstallStatus === 'installing' && app.runtimeInstallBackend === selectedBackend.value)
  && app.runtimeReadyForBackend(selectedBackend.value))
const selectedGpuReport = computed(() => runtimeGpuReportForEnvironment(app.runtimeGpuProbeReport, selectedRuntime.value))
const selectedGpuStatus = computed(() => runtimeGpuStatus(selectedGpuReport.value, selectedRuntime.value?.activationValidation))
const selectedRocmActive = computed(() => selectedBackend.value === 'rocm' && selectedBackendInstalled.value
  && String(app.runtimeInfo?.installedBackend || app.runtimeInfo?.installState?.backend || app.runtimeInstalledBackend) === 'rocm')
const runtimeBusy = computed(() => runtimeChecking.value || app.runtimeGpuProbeStatus === 'running' || app.runtimeMaintenanceStatus === 'running')
const runtimeGpuProgress = computed(() => ['inventory', 'mode', 'check'].includes(app.runtimeGpuProbeStage)
  ? t(`settings.runtimeGpuStage_${app.runtimeGpuProbeStage}`) : app.runtimeGpuProbeStage || t('settings.runtimeGpuChecking'))
const runtimeError = computed(() => runtimeActionError.value || app.runtimeInfoError)
const runtimeStatusKey = computed(() => runtimeBusy.value ? 'onboarding.runtimeChecking'
  : selectedRuntimeBroken.value ? 'settings.runtimeHealthBroken'
  : selectedRocmActive.value ? `settings.runtimeGpuStatus_${selectedGpuStatus.value}`
    : selectedBackendReady.value ? 'onboarding.runtimeReady'
    : selectedBackendInstalled.value ? 'onboarding.runtimeNeedsActivation'
      : app.runtimeManagementSupported ? 'onboarding.runtimeNeedsInstall' : 'onboarding.runtimeUnavailable')
const runtimeRecommendationKey = computed(() => {
  if (selectedRuntimeBroken.value) return 'settings.runtimeHealthBrokenHint'
  if (runtimePlatform.value.isMac) return 'onboarding.runtimeMacHint'
  if (selectedRocmActive.value) return selectedGpuStatus.value === 'unavailable' ? 'settings.runtimeRocmActivationFailed' : 'onboarding.runtimeGpuHint'
  if (selectedBackendReady.value) return 'onboarding.runtimeReadyHint'
  if (selectedBackendInstalled.value) return 'onboarding.runtimeSwitchHint'
  if (runtimeRecommendedBackend.value === 'cuda') return 'onboarding.runtimeNvidiaHint'
  if (runtimeRecommendedBackend.value === 'rocm') return 'onboarding.runtimeAmdHint'
  return 'onboarding.runtimeChooseHint'
})
const primaryActionLabel = computed(() => {
  if (currentStep.value !== 'runtime') return t(currentStep.value === 'welcome' ? 'onboarding.begin' : 'common.next')
  if (runtimeBusy.value) return t('onboarding.runtimeChecking')
  if (selectedBackend.value === 'rocm' && app.runtimeInfo?.seedPreparationRequired) return t('settings.runtimePrepareSeed')
  if (selectedBackendReady.value) return t('onboarding.startUsing')
  return t('onboarding.manageRuntime')
})

const languageOptions = computed(() => [
  { label: t('settings.languageSystem'), value: SYSTEM_LOCALE },
  { label: t('settings.languageSimplifiedChinese'), value: 'zh-CN' },
  { label: t('settings.languageEnglish'), value: 'en' },
])

const themeModeOptions = computed(() => ([
  { label: t('settings.themeSystem'), value: 'system' },
  { label: t('settings.themeDark'), value: 'dark' },
  { label: t('settings.themeLight'), value: 'light' },
]) satisfies Array<{ label: string; value: ThemeMode }>)

const accentOptions = computed(() =>
  THEME_ACCENTS.map((accent) => ({
    value: accent,
    label: t(`settings.themeAccent${accent[0].toUpperCase()}${accent.slice(1)}`),
    preview: getThemeAccentPreview(accent, resolvedIsDark(themeMode.value)),
  })),
)

const stepList = computed(() => [
  {
    key: 'welcome',
    icon: MusicalNotesOutline,
    title: t('onboarding.welcomeTitle'),
    description: t('onboarding.welcomeDescription'),
  },
  {
    key: 'appearance',
    icon: ColorPaletteOutline,
    title: t('onboarding.appearanceTitle'),
    description: t('onboarding.appearanceDescription'),
  },
  {
    key: 'dataDir',
    icon: FolderOpenOutline,
    title: t('onboarding.dataDirTitle'),
    description: t('onboarding.dataDirDescription'),
  },
  {
    key: 'runtime',
    icon: HardwareChipOutline,
    title: t('onboarding.runtimeTitle'),
    description: t(runtimePlatform.value.isMac ? 'onboarding.runtimeMacDescription' : 'onboarding.runtimeDescription'),
  },
])

const totalSteps = computed(() => stepList.value.length)
const stepMeta = computed(() => stepList.value[step.value])
const currentStep = computed(() => stepMeta.value.key)

const canGoNext = computed(() => {
  if (currentStep.value === 'dataDir') {
    return !checkingModelDir.value && !isModelDirMigrating.value && modelDirMigrationState.value.status !== 'confirm'
  }
  if (currentStep.value === 'runtime' && runtimeBusy.value) return false
  return !finishing.value
})
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

function centerOrigin() {
  return { x: window.innerWidth / 2, y: window.innerHeight / 2 }
}

async function selectLocaleOption(value: LocaleSetting) {
  if (locale.value === value) return
  await runRippleViewTransition(() => {
    locale.value = value
    setLocale(value)
  }, centerOrigin())
}

async function selectThemeMode(value: ThemeMode) {
  if (themeMode.value === value) return
  await runRippleViewTransition(() => {
    themeMode.value = value
    applyTheme(value, themeAccent.value)
  }, centerOrigin())
}

async function selectThemeAccent(value: ThemeAccent) {
  if (themeAccent.value === value) return
  await runRippleViewTransition(() => {
    themeAccent.value = value
    applyTheme(themeMode.value, value)
  }, centerOrigin())
}

async function changeModelDir() {
  if (isModelDirMigrating.value) return
  const folder = await settings.pickModelDir()
  if (!folder) return
  checkingModelDir.value = true
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
    checkingModelDir.value = false
  }
}

async function changeOutputDir() {
  const folder = await settings.pickFolder()
  if (folder) outputDir.value = folder
}

async function openProjectUrl(url: string) {
  try {
    if (isTauriRuntime()) {
      await open(url)
    } else {
      window.open(url, '_blank', 'noopener,noreferrer')
    }
  } catch {
    message.error(t('onboarding.projectLinkFailed'))
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

async function nextStep() {
  if (step.value < totalSteps.value - 1) {
    step.value += 1
    return true
  }
  return false
}

function previousStep() {
  if (step.value <= 0 || runtimeBusy.value || finishing.value) return
  step.value -= 1
}

async function refreshRuntime() {
  if (runtimeBusy.value || app.runtimeInstallStatus === 'installing') return
  runtimeChecking.value = true
  runtimeActionError.value = ''
  try {
    await app.checkRuntimeInfo()
  } catch (error) {
    runtimeActionError.value = error instanceof Error ? error.message : String(error)
  } finally {
    runtimeChecking.value = false
  }
}

async function probeRuntimeGpu() {
  if (runtimeBusy.value) return
  runtimeActionError.value = ''
  try { await app.startRuntimeGpuProbe() } catch (error) { runtimeActionError.value = error instanceof Error ? error.message : String(error) }
}

async function cancelRuntimeGpuProbe() {
  try { await app.cancelRuntimeGpuProbe() } catch (error) { runtimeActionError.value = error instanceof Error ? error.message : String(error) }
}

async function finishOnboarding(event?: MouseEvent, skipRuntime = false, manageRuntime = false) {
  if (finishing.value || runtimeBusy.value || !canGoNext.value || (!skipRuntime && !selectedBackendReady.value)) return
  finishing.value = true
  try {
    const destination = manageRuntime
      ? { name: 'settings', query: { section: 'runtime', backend: selectedBackend.value } }
      : { name: 'separate' }
    const origin = event ? { x: event.clientX, y: event.clientY } : centerOrigin()
    await runRippleViewTransition(async () => {
      await router.push(destination)
      await settings.markStartupOnboardingCompleted()
      emit('completed')
    }, origin)
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    finishing.value = false
  }
}

function primaryAction(event: MouseEvent) {
  if (!canGoNext.value) return
  if (currentStep.value !== 'runtime') return nextStep()
  if (selectedBackendReady.value) return finishOnboarding(event)
  return finishOnboarding(event, true, true)
}

function trapFocus(event: KeyboardEvent) {
  if (event.key !== 'Tab') return
  const focusable = Array.from(overlayRef.value?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), [tabindex="0"], summary') || [])
    .filter(element => element.getClientRects().length > 0 && !element.closest('[inert]'))
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last?.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first?.focus()
  }
}

onMounted(() => {
  void refreshRuntime()
  void nextTick(() => overlayRef.value?.querySelector<HTMLElement>('[tabindex="0"], button')?.focus())
})
</script>

<template>
  <div ref="overlayRef" class="onboarding-overlay" role="dialog" aria-modal="true" aria-labelledby="onboarding-title" @keydown="trapFocus">
    <div class="onboarding-card">
      <aside class="onboarding-rail">
        <div class="onboarding-brand">
          <AppBrandMark :size="42" variant="hero" shadow />
          <div><strong>Pymss Studio</strong><span>{{ t('onboarding.title') }}</span></div>
        </div>
        <ol class="onboarding-nav" :aria-label="t('onboarding.title')">
          <li v-for="(meta, index) in stepList" :key="meta.key" class="onboarding-nav__item" :class="{ active: index === step, done: index < step }" :aria-current="index === step ? 'step' : undefined">
            <span class="onboarding-nav__marker"><n-icon :component="index < step ? CheckmarkOutline : meta.icon" size="17" /></span>
            <span>{{ meta.title }}</span>
          </li>
        </ol>
        <p class="onboarding-rail__hint">{{ t('onboarding.subtitle') }}</p>
      </aside>

      <section class="onboarding-content">
        <header class="onboarding-content__head">
          <div class="onboarding-heading">
            <span class="onboarding-content__counter">{{ t('onboarding.stepCounter', { current: step + 1, total: totalSteps }) }}</span>
            <h1 id="onboarding-title">{{ currentStep === 'welcome' ? t('onboarding.welcomeHeadline') : stepMeta.title }}</h1>
          </div>
          <n-select class="onboarding-language" :value="locale" :options="languageOptions" :disabled="finishing" :aria-label="t('onboarding.languageTitle')" :to="overlayRef || undefined" @update:value="selectLocaleOption" />
          <p>{{ stepMeta.description }}</p>
        </header>

        <div class="onboarding-content__body">
          <section v-if="currentStep === 'welcome'" class="onboarding-welcome">
            <div class="onboarding-audio-flow" aria-hidden="true">
              <div class="onboarding-audio-flow__input">
                <n-icon :component="MusicalNotesOutline" size="22" />
                <span>{{ t('onboarding.audioInput') }}</span>
                <div class="onboarding-waveform"><i v-for="(height, index) in waveform" :key="index" :style="{ height: height + '%' }" /></div>
              </div>
              <n-icon class="onboarding-audio-flow__arrow" :component="ArrowForwardOutline" size="24" />
              <div class="onboarding-audio-flow__stems">
                <div><span>{{ t('onboarding.vocals') }}</span><div class="onboarding-waveform"><i v-for="(height, index) in waveform.slice(0, 24)" :key="index" :style="{ height: height * 0.7 + '%' }" /></div></div>
                <div><span>{{ t('onboarding.instrumental') }}</span><div class="onboarding-waveform"><i v-for="(height, index) in waveform.slice(8, 32)" :key="index" :style="{ height: height + '%' }" /></div></div>
              </div>
            </div>
            <div class="onboarding-project__notice">
              <strong>{{ t('onboarding.projectHeadline') }}</strong>
              <p>{{ t('onboarding.projectNotice') }}</p>
              <p>{{ t('onboarding.projectDownloadHint') }}</p>
            </div>
            <a class="onboarding-project__repository" :href="projectRepositoryUrl" :aria-label="t('onboarding.projectVisitRepository')" target="_blank" rel="noopener noreferrer" @click.prevent="openProjectUrl(projectRepositoryUrl)">
              <n-icon :component="LogoGithub" size="20" /><span>{{ t('onboarding.projectRepositoryLabel') }}</span><n-icon :component="OpenOutline" size="15" />
            </a>
          </section>

          <section v-else-if="currentStep === 'appearance'" class="onboarding-appearance">
            <div class="onboarding-appearance__controls">
              <fieldset class="onboarding-fieldset">
                <legend>{{ t('onboarding.themeTitle') }}</legend>
                <div class="onboarding-chip-grid" role="group" :aria-label="t('onboarding.themeTitle')">
                  <button v-for="item in themeModeOptions" :key="item.value" type="button" class="onboarding-chip" :class="{ active: themeMode === item.value }" :aria-pressed="themeMode === item.value" @click="selectThemeMode(item.value)">{{ item.label }}</button>
                </div>
              </fieldset>
              <fieldset class="onboarding-fieldset">
                <legend>{{ t('onboarding.accentTitle') }}</legend>
                <div class="onboarding-accent-grid" role="group" :aria-label="t('onboarding.accentTitle')">
                  <button v-for="accent in accentOptions" :key="accent.value" type="button" class="onboarding-accent-card" :class="{ active: themeAccent === accent.value }" :aria-pressed="themeAccent === accent.value" @click="selectThemeAccent(accent.value)">
                    <span class="onboarding-accent-card__swatches"><i v-for="swatch in accent.preview" :key="swatch" :style="{ background: swatch }" /></span><span>{{ accent.label }}</span>
                  </button>
                </div>
              </fieldset>
            </div>
            <figure class="onboarding-preview" :aria-label="t('onboarding.appearancePreview')">
              <div class="onboarding-preview__workspace">
                <strong>{{ t('onboarding.appearancePreview') }}</strong>
                <div class="onboarding-preview__selection"><span>{{ t('nav.separate') }}</span><n-icon :component="CheckmarkOutline" size="17" /></div>
                <div class="onboarding-preview__field"><span>{{ t('tools.outputFormat') }}</span><strong>{{ settings.defaultFormat.toUpperCase() }}</strong></div>
                <div class="onboarding-preview__statuses"><span>{{ t('tasks.statusQueued') }}</span><span>{{ t('tasks.statusDone') }}</span></div>
                <div class="onboarding-preview__actions"><span>{{ t('common.cancel') }}</span><span class="onboarding-preview__action">{{ t('common.confirm') }}</span></div>
              </div>
              <figcaption>{{ t('onboarding.appearancePreviewHint') }}</figcaption>
            </figure>
          </section>

          <section v-else-if="currentStep === 'dataDir'" class="onboarding-directories">
            <div class="onboarding-directory">
              <n-icon :component="FolderOpenOutline" size="22" />
              <div><strong>{{ t('onboarding.currentModelDir') }}</strong><p>{{ t('onboarding.modelDirHint') }}</p></div>
              <n-button secondary :loading="checkingModelDir" :disabled="isModelDirMigrating" @click="changeModelDir">{{ t('onboarding.changeDirectory') }}</n-button>
              <code :title="modelDir">{{ modelDir || t('onboarding.defaultDirectory') }}</code>
            </div>
            <div class="onboarding-directory">
              <n-icon :component="FolderOpenOutline" size="22" />
              <div><strong>{{ t('onboarding.currentOutputDir') }}</strong><p>{{ t('onboarding.outputDirHint') }}</p></div>
              <n-button secondary @click="changeOutputDir">{{ t('onboarding.changeDirectory') }}</n-button>
              <code :title="outputDir">{{ outputDir || t('onboarding.defaultDirectory') }}</code>
            </div>
            <p class="onboarding-hint">{{ t('onboarding.dataDirReady') }}</p>
          </section>

          <section v-else-if="currentStep === 'runtime'" class="onboarding-runtime-section" :aria-busy="runtimeBusy">
            <div class="onboarding-runtime-status" :class="{ 'is-ready': selectedBackendReady && !runtimeBusy }" role="status" aria-live="polite">
              <n-spin v-if="runtimeBusy" size="small" />
              <n-icon v-else :component="selectedBackendReady ? CheckmarkOutline : HardwareChipOutline" size="24" />
              <div><strong>{{ t(runtimeStatusKey) }}</strong><p>{{ t(runtimeRecommendationKey) }}</p></div>
              <n-button v-if="!runtimeBusy" quaternary circle :aria-label="t('onboarding.checkAgain')" @click="refreshRuntime"><template #icon><n-icon :component="RefreshOutline" /></template></n-button>
            </div>

            <div v-if="app.runtimeManagementSupported" class="onboarding-runtime-grid" role="group" :aria-label="t('onboarding.runtimeTitle')">
              <button v-for="choice in runtimeBackendChoices" :key="choice.value" type="button" class="onboarding-runtime-card" :class="{ active: selectedBackend === choice.value }" :aria-pressed="selectedBackend === choice.value" :disabled="runtimeBusy || finishing" @click="selectedBackend = choice.value; backendTouched = true; runtimeActionError = ''">
                <strong>{{ choice.label }}</strong>
                <span class="onboarding-runtime-card__tags"><n-tag v-if="choice.installed" :bordered="false" size="small" type="success">{{ t('onboarding.runtimeInstalledTag') }}</n-tag><n-tag v-if="choice.recommended" :bordered="false" size="small">{{ t('onboarding.runtimeRecommendedTag') }}</n-tag></span>
                <span>{{ choice.hint }}</span><small v-if="!choice.installed">{{ t('onboarding.runtimeSizeHint', { size: choice.size }) }}</small>
              </button>
            </div>
            <div v-else class="onboarding-runtime-single">
              <n-icon :component="HardwareChipOutline" size="26" />
              <div><strong>{{ runtimePlatform.isMac ? 'Apple MLX' : runtimeBackendName(selectedBackend) }}</strong><p>{{ t(runtimePlatform.isMac ? 'onboarding.runtimeMlx' : 'onboarding.runtimeReadOnlyHint') }}</p></div>
              <n-tag :bordered="false" :type="selectedBackendReady ? 'success' : 'warning'">{{ t(selectedBackendReady ? 'onboarding.runtimeReady' : 'onboarding.runtimeUnavailable') }}</n-tag>
            </div>

            <div v-if="selectedBackendReady && !runtimeBusy" class="onboarding-readiness">
              <div><span>{{ t('onboarding.runtimeVersion') }}</span><strong>{{ selectedRuntime?.pymssVersion || app.runtimeInfo?.pymssVersion || '—' }}</strong></div>
              <p>{{ t('onboarding.readyToUse') }}</p>
            </div>
            <div v-if="selectedRocmActive" class="onboarding-readiness">
              <n-button v-if="app.runtimeGpuProbeStatus !== 'running'" size="small" :disabled="runtimeBusy" @click="probeRuntimeGpu">{{ t('settings.runtimeGpuCheck') }}</n-button>
              <template v-else><span role="status" aria-live="polite">{{ runtimeGpuProgress }}</span><n-button size="small" @click="cancelRuntimeGpuProbe">{{ t('common.cancel') }}</n-button></template>
              <p v-if="app.runtimeGpuProbeStatus === 'error'" class="onboarding-runtime-error">{{ runtimeErrorSummary(app.runtimeGpuProbeError, t) }}</p>
            </div>

            <p v-if="!selectedBackendReady && !runtimeBusy" class="onboarding-hint">{{ t(app.runtimeManagementSupported ? 'onboarding.runtimeManageHint' : 'onboarding.runtimeReadOnlyHint') }}</p>
            <n-alert v-if="runtimeError" type="error" :show-icon="true"><template #header>{{ t('onboarding.runtimeFailed') }}</template><p class="onboarding-runtime-error">{{ runtimeError }}</p></n-alert>
          </section>
        </div>

        <footer class="onboarding-content__foot">
          <n-button v-if="step > 0" quaternary :disabled="runtimeBusy || finishing" @click="previousStep">{{ t('common.back') }}</n-button>
          <span class="onboarding-content__spacer" />
          <n-button v-if="currentStep === 'runtime' && !selectedBackendReady" quaternary :disabled="runtimeBusy || finishing" @click="finishOnboarding($event, true)">{{ t('onboarding.setUpLater') }}</n-button>
          <n-button type="primary" :disabled="!canGoNext" :loading="finishing || (currentStep === 'runtime' && runtimeBusy)" @click="primaryAction">{{ primaryActionLabel }}<template v-if="!runtimeBusy && !finishing" #icon><n-icon :component="ArrowForwardOutline" /></template></n-button>
        </footer>
      </section>
    </div>


    <n-modal
      :show="checkingModelDir"
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
  </div>
</template>

<style scoped>
.onboarding-overlay {
  position: absolute;
  inset: 0;
  z-index: 3200;
  display: grid;
  place-items: center;
  padding: 32px;
  background: color-mix(in srgb, var(--surface) 94%, transparent);
  backdrop-filter: blur(18px);
}
.onboarding-card {
  display: grid;
  grid-template-columns: 218px minmax(0, 1fr);
  width: min(1040px, 100%);
  height: min(640px, 100%);
  min-height: 0;
  overflow: hidden;
  border: 1px solid var(--outline);
  border-radius: 22px;
  background: var(--surface-1);
  box-shadow: var(--shadow-panel);
}
.onboarding-rail {
  display: flex;
  flex-direction: column;
  gap: 42px;
  min-width: 0;
  padding: 28px 18px 24px;
  border-right: 1px solid var(--outline);
  background: color-mix(in srgb, var(--surface-2) 52%, var(--surface-1));
}
.onboarding-brand { display: flex; align-items: center; gap: 10px; padding: 0 2px; }
.onboarding-brand > div { display: grid; gap: 4px; }
.onboarding-brand strong { font-size: 15px; color: var(--on-surface-heading); }
.onboarding-brand span { font-size: 11px; color: var(--on-surface-muted); }
.onboarding-nav { display: grid; gap: 8px; list-style: none; margin: 0; padding: 0; }
.onboarding-nav__item { display: flex; align-items: center; gap: 12px; padding: 12px 10px; color: var(--on-surface-muted); font-size: 13px; border-radius: 10px; }
.onboarding-nav__marker { display: grid; place-items: center; width: 28px; height: 28px; flex: none; border: 1px solid var(--outline); border-radius: 8px; }
.onboarding-nav__item.active { background: var(--primary-softer); color: var(--on-surface-heading); }
.onboarding-nav__item.active .onboarding-nav__marker { border-color: var(--primary-border); color: var(--primary-strong); background: var(--primary-soft); }
.onboarding-nav__item.done .onboarding-nav__marker { color: var(--success); border-color: transparent; }
.onboarding-rail__hint { margin: auto 8px 0; color: var(--on-surface-muted); font-size: 12px; line-height: 1.8; }
.onboarding-content { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; min-width: 0; min-height: 0; padding: 30px 34px 22px; }
.onboarding-content__head { display: grid; grid-template-columns: minmax(0, 1fr) 156px; align-items: start; gap: 14px 16px; padding-bottom: 22px; }
.onboarding-heading { display: grid; gap: 8px; }
.onboarding-content__counter { color: var(--on-surface-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.onboarding-content__head h1 { margin: 0; color: var(--on-surface-heading); font-size: 27px; line-height: 1.35; letter-spacing: -0.025em; text-wrap: balance; }
.onboarding-content__head p { grid-column: 1 / -1; margin: 0; max-width: 60ch; font-size: 13px; line-height: 1.75; color: var(--on-surface-muted); }
.onboarding-language { margin-top: 10px; }
.onboarding-content__body { min-height: 0; overflow: auto; scrollbar-width: thin; scrollbar-color: var(--outline-strong) transparent; padding: 2px 2px 16px; }
.onboarding-content__foot { display: flex; align-items: center; gap: 10px; padding-top: 16px; border-top: 1px solid var(--outline); }
.onboarding-content__spacer { flex: 1; }
.onboarding-hint { margin: 0; font-size: 12px; line-height: 1.75; color: var(--on-surface-muted); }
.onboarding-welcome { display: grid; gap: 28px; }
.onboarding-audio-flow { display: grid; grid-template-columns: minmax(0, 1fr) 24px minmax(0, 1fr); align-items: center; gap: 18px; padding: 26px 22px; border-radius: 14px; background: var(--surface-2); }
.onboarding-audio-flow__input { display: grid; grid-template-columns: 24px 1fr; align-items: center; gap: 10px; font-size: 13px; }
.onboarding-audio-flow__input > .n-icon { color: var(--primary-strong); }
.onboarding-audio-flow__input > .onboarding-waveform { grid-column: 1 / -1; height: 70px; margin-top: 8px; }
.onboarding-waveform { display: flex; align-items: center; justify-content: space-between; gap: 3px; height: 50px; overflow: hidden; }
.onboarding-waveform i { flex: 1; min-width: 2px; max-width: 5px; border-radius: 3px; background: var(--primary); opacity: 0.8; }
.onboarding-audio-flow__arrow { color: var(--on-surface-muted); }
.onboarding-audio-flow__stems { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 14px; align-items: center; font-size: 12px; }
.onboarding-audio-flow__stems > div { display: contents; }
.onboarding-audio-flow__stems .onboarding-waveform { height: 44px; }
.onboarding-audio-flow__stems > div:last-child i { opacity: 0.4; }
.onboarding-project__notice strong { display: block; margin-bottom: 10px; color: var(--on-surface-heading); font-size: 15px; }
.onboarding-project__notice p { margin: 6px 0 0; color: var(--on-surface-muted); font-size: 13px; line-height: 1.85; }
.onboarding-project__repository { display: inline-flex; align-items: center; justify-self: start; gap: 10px; color: var(--primary-strong); font-size: 12px; padding: 6px 0; }
.onboarding-appearance { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 0.95fr); align-items: start; gap: 26px; }
.onboarding-appearance__controls { display: grid; gap: 28px; }
.onboarding-fieldset { border: 0; padding: 0; margin: 0; min-width: 0; }
.onboarding-fieldset legend { margin-bottom: 12px; color: var(--on-surface); font-size: 12px; }
.onboarding-chip-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; }
.onboarding-chip, .onboarding-accent-card { border: 1px solid var(--outline); background: var(--surface-2); color: var(--on-surface-muted); border-radius: 8px; cursor: pointer; transition: border-color 160ms, background 160ms; }
.onboarding-chip { padding: 11px 4px; min-width: 0; font-size: 12px; }
.onboarding-chip.active, .onboarding-accent-card.active, .onboarding-runtime-card.active { border-color: var(--primary-border); background: var(--primary-softer); color: var(--on-surface); }
.onboarding-chip:hover, .onboarding-accent-card:hover, .onboarding-runtime-card:hover:not(:disabled) { border-color: var(--primary); }
.onboarding-accent-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.onboarding-accent-card { display: flex; align-items: center; gap: 8px; padding: 11px 10px; font-size: 12px; text-align: left; }
.onboarding-accent-card__swatches { display: flex; gap: 3px; }
.onboarding-accent-card__swatches i { width: 10px; height: 18px; border-radius: 3px; border: 1px solid var(--outline); }
.onboarding-preview { margin: 0; overflow: hidden; border: 1px solid var(--outline); border-radius: 12px; background: var(--surface); }
.onboarding-preview__workspace { display: grid; gap: 20px; padding: 22px 20px; }
.onboarding-preview__workspace > strong { font-size: 13px; color: var(--on-surface-heading); }
.onboarding-preview__selection { display: flex; justify-content: space-between; align-items: center; padding: 12px; border: 1px solid var(--primary-border); border-radius: 8px; background: var(--primary-softer); color: var(--primary-strong); font-size: 12px; }
.onboarding-preview__field { display: flex; justify-content: space-between; align-items: center; font-size: 12px; }
.onboarding-preview__field > span { color: var(--on-surface-muted); }
.onboarding-preview__field > strong { padding: 7px 10px; border: 1px solid var(--outline); border-radius: 6px; font-size: 11px; }
.onboarding-preview__statuses { display: flex; gap: 8px; font-size: 11px; }
.onboarding-preview__statuses > span { padding: 4px 8px; border-radius: 4px; background: var(--surface-2); color: var(--on-surface-muted); }
.onboarding-preview__statuses > span:last-child { background: color-mix(in srgb, var(--success) 12%, transparent); color: var(--success); }
.onboarding-preview__actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 2px; text-align: center; font-size: 12px; }
.onboarding-preview__actions > span { padding: 9px 12px; border: 1px solid var(--outline); border-radius: 6px; }
.onboarding-preview__actions > .onboarding-preview__action { border-color: transparent; background: var(--primary); color: var(--surface); }
.onboarding-preview figcaption { padding: 0 20px 18px; color: var(--on-surface-muted); font-size: 11px; line-height: 1.6; }
.onboarding-directories { display: grid; gap: 20px; }
.onboarding-directory { display: grid; grid-template-columns: 24px minmax(0, 1fr) auto; gap: 12px 14px; align-items: center; padding: 20px; border: 1px solid var(--outline); border-radius: 12px; }
.onboarding-directory > .n-icon { color: var(--primary-strong); }
.onboarding-directory strong { font-size: 14px; color: var(--on-surface-heading); }
.onboarding-directory p { margin: 5px 0 0; font-size: 12px; line-height: 1.6; color: var(--on-surface-muted); }
.onboarding-directory code { grid-column: 2 / -1; min-width: 0; padding: 10px 12px; background: var(--surface-2); border-radius: 6px; font-size: 12px; overflow-wrap: anywhere; }
.onboarding-runtime-section { display: grid; gap: 18px; }
.onboarding-runtime-status { display: grid; grid-template-columns: 26px minmax(0, 1fr) auto; align-items: center; gap: 12px; padding: 16px 18px; border: 1px solid var(--outline); border-radius: 12px; background: var(--surface-2); }
.onboarding-runtime-status.is-ready { border-color: color-mix(in srgb, var(--success) 30%, var(--outline)); }
.onboarding-runtime-status.is-ready > .n-icon { color: var(--success); }
.onboarding-runtime-status strong { color: var(--on-surface-heading); font-size: 14px; }
.onboarding-runtime-status p { margin: 5px 0 0; color: var(--on-surface-muted); line-height: 1.65; font-size: 12px; }
.onboarding-runtime-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.onboarding-runtime-card { display: flex; flex-direction: column; align-items: start; gap: 10px; min-width: 0; padding: 16px 14px; border: 1px solid var(--outline); border-radius: 10px; background: transparent; color: var(--on-surface); text-align: left; cursor: pointer; transition: border-color 160ms, background 160ms; }
.onboarding-runtime-card:disabled { opacity: 0.6; cursor: default; }
.onboarding-runtime-card strong { font-size: 13px; }
.onboarding-runtime-card > span:not(.onboarding-runtime-card__tags), .onboarding-runtime-card small { color: var(--on-surface-muted); font-size: 11px; line-height: 1.6; }
.onboarding-runtime-card small { margin-top: auto; }
.onboarding-runtime-card__tags { display: flex; flex-wrap: wrap; gap: 4px; }
.onboarding-runtime-card__tags:empty { display: none; }
.onboarding-runtime-single { display: flex; align-items: center; gap: 14px; padding: 20px 4px; }
.onboarding-runtime-single > .n-icon { color: var(--primary-strong); }
.onboarding-runtime-single > div:not(.n-tag) { flex: 1; }
.onboarding-runtime-single strong { color: var(--on-surface-heading); font-size: 17px; }
.onboarding-runtime-single p { margin: 6px 0 0; color: var(--on-surface-muted); font-size: 12px; }
.onboarding-readiness { display: grid; gap: 12px; padding: 0 4px; }
.onboarding-readiness > div { display: flex; justify-content: space-between; gap: 12px; font-size: 12px; }
.onboarding-readiness > div > span { color: var(--on-surface-muted); }
.onboarding-readiness strong { font-size: 12px; }
.onboarding-readiness > p { margin: 3px 0 0; color: var(--on-surface-muted); font-size: 12px; line-height: 1.75; }
.onboarding-runtime-error { margin: 0 0 8px; overflow-wrap: anywhere; white-space: pre-wrap; }
.onboarding-overlay :is(button, a, summary):focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
@media (max-width: 1000px) {
  .onboarding-overlay { padding: 20px; }
  .onboarding-card { grid-template-columns: 190px minmax(0, 1fr); }
  .onboarding-content { padding: 24px 26px 20px; }
  .onboarding-appearance { gap: 18px; }
  .onboarding-accent-card { flex-wrap: wrap; }
}
@media (max-width: 760px) {
  .onboarding-overlay { padding: 12px; }
  .onboarding-card { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); height: 100%; }
  .onboarding-rail { padding: 12px; border-right: 0; border-bottom: 1px solid var(--outline); }
  .onboarding-brand, .onboarding-rail__hint { display: none; }
  .onboarding-nav { grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 4px; }
  .onboarding-nav__item { flex-direction: column; gap: 6px; padding: 8px 2px; font-size: 11px; text-align: center; }
  .onboarding-content { padding: 20px 18px 16px; }
  .onboarding-content__head { grid-template-columns: minmax(0, 1fr) 140px; }
  .onboarding-content__head h1 { font-size: 23px; }
  .onboarding-appearance { grid-template-columns: minmax(0, 1fr); }
  .onboarding-preview { max-width: 360px; width: 100%; justify-self: center; }
  .onboarding-runtime-grid { grid-template-columns: minmax(0, 1fr); }
  .onboarding-runtime-card { display: grid; grid-template-columns: 1fr auto; }
  .onboarding-runtime-card > span:not(.onboarding-runtime-card__tags) { grid-column: 1 / -1; }
  .onboarding-content__foot { gap: 5px; flex-wrap: wrap; }
  .onboarding-directory { padding: 14px; gap: 10px; }
  .onboarding-directory code { grid-column: 1 / -1; }
  .onboarding-audio-flow { padding: 18px 12px; gap: 10px; }
}
@media (prefers-reduced-motion: reduce) {
  .onboarding-overlay *, .onboarding-overlay *::before, .onboarding-overlay *::after { transition: none !important; }
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

</style>
