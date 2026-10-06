<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { useRouter } from 'vue-router'
import { CheckmarkOutline, ColorPaletteOutline, FolderOpenOutline, HardwareChipOutline, LanguageOutline, MoonOutline } from '@vicons/ionicons5'
import AppBrandMark from '@/components/AppBrandMark.vue'
import { SYSTEM_LOCALE, setLocale, type LocaleSetting } from '@/i18n'
import { useSettingsStore } from '@/stores/settings'
import { useAppStore, type RuntimeBackend } from '@/stores/app'
import { formatBytes } from '@/utils/format'
import {
  detectRuntimePlatform,
  preferredInstalledRuntimeBackend,
  recommendedRuntimeBackend,
  runtimeBackendLabel as runtimeBackendName,
  runtimeSizeHint,
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

const { t, locale: currentLocale } = useI18n()
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
const totalSteps = 5
const checkingModelDir = ref(false)
const overlayRef = ref<HTMLElement | null>(null)
const completeButtonRef = ref<HTMLElement | null>(null)
const selectedBackend = ref<RuntimeBackend>('cpu')
const runtimeMirror = ref('auto')
const runtimeChecking = ref(false)

const runtimePlatform = computed(() => detectRuntimePlatform(app.runtimeInfo))

// An environment counts as installed even when it is not the active one, so this reads the
// installed list rather than app.runtimeReadyForBackend() (which only answers for the active backend).
function backendInstalled(backend: RuntimeBackend) {
  return (app.runtimeInfo?.installedEnvironments || []).some((entry) => entry.backend === backend)
}

const runtimeBackendChoices = computed(() => {
  const { isMac, isWindows, isAppleSilicon } = runtimePlatform.value
  const items: Array<{ value: RuntimeBackend; label: string; hint: string }> = [
    { value: 'cpu', label: runtimeBackendName('cpu'), hint: t('onboarding.runtimeCpu') },
  ]
  if (!isMac) {
    items.push(
      { value: 'cuda', label: runtimeBackendName('cuda'), hint: t('onboarding.runtimeCuda') },
      { value: 'rocm', label: runtimeBackendName('rocm'), hint: t('onboarding.runtimeRocm') },
    )
  }
  if (isWindows) {
    items.push({ value: 'dml', label: runtimeBackendName('dml'), hint: t('onboarding.runtimeDml') })
  }
  if (isAppleSilicon) {
    items.push({ value: 'mlx', label: runtimeBackendName('mlx'), hint: t('onboarding.runtimeMlx') })
  }
  return items.map((item) => ({
    ...item,
    installed: backendInstalled(item.value),
    recommended: runtimeRecommendedBackend.value === item.value,
    size: runtimeSizeHint(item.value),
  }))
})

const runtimeRecommendedBackend = computed(() => recommendedRuntimeBackend(app.runtimeInfo))
const preferredRuntimeBackend = computed(() => preferredInstalledRuntimeBackend(app.runtimeInfo) || runtimeRecommendedBackend.value)

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
const selectedBackendSize = computed(() => runtimeSizeHint(selectedBackend.value))

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
    icon: LanguageOutline,
    title: t('onboarding.languageTitle'),
    description: t('onboarding.languageDescription'),
  },
  {
    icon: MoonOutline,
    title: t('onboarding.themeTitle'),
    description: t('onboarding.themeDescription'),
  },
  {
    icon: ColorPaletteOutline,
    title: t('onboarding.accentTitle'),
    description: t('onboarding.accentDescription'),
  },
  {
    icon: FolderOpenOutline,
    title: t('onboarding.dataDirTitle'),
    description: t('onboarding.dataDirDescription'),
  },
  {
    icon: HardwareChipOutline,
    title: t('onboarding.runtimeTitle'),
    description: t('onboarding.runtimeDescription'),
  },
])

const stepMeta = computed(() => stepList.value[step.value])

const dataDirStepIndex = 3
const runtimeStepIndex = 4

const canGoNext = computed(() => {
  if (step.value === dataDirStepIndex) {
    return !checkingModelDir.value && !isModelDirMigrating.value && modelDirMigrationState.value.status !== 'confirm'
  }
  if (step.value === runtimeStepIndex && runtimeChecking.value) return false
  return true
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
  if (step.value < totalSteps - 1) {
    step.value += 1
    return true
  }
  return false
}

function previousStep() {
  if (step.value <= 0) return
  step.value -= 1
}

function getElementOrigin(element: HTMLElement | null) {
  if (!element) return null
  const rect = element.getBoundingClientRect()
  return {
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
  }
}

async function finishOnboarding(event?: MouseEvent) {
  const origin = event
    ? { x: event.clientX, y: event.clientY }
    : getElementOrigin(completeButtonRef.value) || {
        x: window.innerWidth / 2,
        y: window.innerHeight / 2,
  }
  const backend = selectedBackend.value
  const alreadyReady = app.runtimeReadyForBackend(backend)
  const alreadyInstalled = backendInstalled(backend)
  const needsRuntimeSettings = !alreadyReady
  await runRippleViewTransition(async () => {
    await settings.markStartupOnboardingCompleted()
    if (needsRuntimeSettings) {
      await router.push({ name: 'settings', query: { section: 'runtime' } })
    }
    emit('completed')
  }, origin)
  // Runtime errors are handled by the store and surfaced in Settings.
  if (alreadyReady) return
  if (alreadyInstalled) {
    // Installed but not active — switching is enough, no need to download it again.
    const installed = app.runtimeInfo?.installedEnvironments?.find((entry) => entry.backend === backend)
    app.activateRuntime(backend, { pythonPath: installed?.pythonPath }).catch(() => {})
  } else {
    app.installRuntime(backend, runtimeMirror.value, currentLocale.value).catch(() => {})
  }
}

onMounted(() => {
  runtimeChecking.value = true
  app.checkRuntimeInfo()
    .catch(() => {})
    .finally(() => {
      runtimeChecking.value = false
    })
  void app.loadRuntimeCoreVersions().catch(() => {})
})
</script>

<template>
  <div ref="overlayRef" class="onboarding-overlay">
    <div class="onboarding-card">
      <aside class="onboarding-rail">
        <div class="onboarding-brand">
          <AppBrandMark :size="46" variant="hero" shadow />
          <div class="onboarding-brand__text">
            <strong>{{ t('onboarding.title') }}</strong>
            <span>{{ t('onboarding.subtitle') }}</span>
          </div>
        </div>

        <ol class="onboarding-nav">
          <li
            v-for="(meta, index) in stepList"
            :key="index"
            class="onboarding-nav__item"
            :class="{ active: index === step, done: index < step }"
          >
            <span class="onboarding-nav__marker">
              <n-icon :component="index < step ? CheckmarkOutline : meta.icon" size="16" />
            </span>
            <span class="onboarding-nav__label">{{ meta.title }}</span>
          </li>
        </ol>
      </aside>

      <section class="onboarding-content">
        <header class="onboarding-content__head">
          <span class="onboarding-content__counter">
            {{ t('onboarding.stepCounter', { current: step + 1, total: totalSteps }) }}
          </span>
          <h1>{{ stepMeta.title }}</h1>
          <p>{{ stepMeta.description }}</p>
        </header>

        <div class="onboarding-content__body">
          <section v-if="step === 0" class="onboarding-section">
            <n-select :value="locale" :options="languageOptions" size="large" :to="overlayRef || undefined" @update:value="selectLocaleOption" />
            <p v-if="locale === SYSTEM_LOCALE" class="onboarding-hint">
              {{ t('settings.languageFollowSystemHint', { locale: currentLocale === 'en' ? t('settings.languageEnglish') : t('settings.languageSimplifiedChinese') }) }}
            </p>
          </section>

          <section v-else-if="step === 1" class="onboarding-section onboarding-chip-grid">
            <button
              v-for="item in themeModeOptions"
              :key="item.value"
              type="button"
              class="onboarding-chip"
              :class="{ active: themeMode === item.value }"
              @click="selectThemeMode(item.value)"
            >
              {{ item.label }}
            </button>
          </section>

          <section v-else-if="step === 2" class="onboarding-section onboarding-accent-grid">
            <button
              v-for="accent in accentOptions"
              :key="accent.value"
              type="button"
              class="onboarding-accent-card"
              :class="{ active: themeAccent === accent.value }"
              @click="selectThemeAccent(accent.value)"
            >
              <span class="onboarding-accent-card__swatches">
                <span
                  v-for="swatch in accent.preview"
                  :key="swatch"
                  class="onboarding-accent-card__swatch"
                  :style="{ background: swatch }"
                />
              </span>
              <span class="onboarding-accent-card__label">{{ accent.label }}</span>
            </button>
          </section>

          <section v-else-if="step === dataDirStepIndex" class="onboarding-section">
            <div class="onboarding-path-card">
              <span>{{ t('onboarding.currentModelDir') }}</span>
              <code :title="modelDir">{{ modelDir }}</code>
            </div>
            <div class="onboarding-path-card__actions">
              <n-button secondary :loading="checkingModelDir" :disabled="isModelDirMigrating" @click="changeModelDir">
                {{ t('onboarding.chooseModelDirectory') }}
              </n-button>
            </div>

            <div class="onboarding-path-card">
              <span>{{ t('onboarding.currentOutputDir') }}</span>
              <code :title="outputDir">{{ outputDir }}</code>
            </div>
            <div class="onboarding-path-card__actions">
              <n-button secondary @click="changeOutputDir">
                {{ t('onboarding.chooseOutputDirectory') }}
              </n-button>
              <p class="onboarding-hint">{{ t('onboarding.dataDirReady') }}</p>
            </div>
          </section>

          <section v-else class="onboarding-section onboarding-runtime-section">
            <div v-if="runtimeChecking" class="onboarding-runtime-locked">
              <n-spin size="small" />
              <div class="onboarding-runtime-locked__copy">
                <strong>{{ t('settings.checkingEnv') }}</strong>
                <span>{{ t('onboarding.runtimeDetecting') }}</span>
              </div>
            </div>

            <div class="onboarding-runtime-grid">
              <button
                v-for="choice in runtimeBackendChoices"
                :key="choice.value"
                type="button"
                class="onboarding-runtime-card"
                :class="{ active: selectedBackend === choice.value }"
                @click="selectedBackend = choice.value; backendTouched = true"
              >
                <span class="onboarding-runtime-card__title">
                  <strong class="onboarding-runtime-card__label">{{ choice.label }}</strong>
                  <n-tag v-if="choice.installed" :bordered="false" size="small" type="success" round>
                    {{ t('onboarding.runtimeInstalledTag') }}
                  </n-tag>
                  <n-tag v-if="choice.recommended" :bordered="false" size="small" type="warning" round>
                    {{ t('onboarding.runtimeRecommendedTag') }}
                  </n-tag>
                </span>
                <span class="onboarding-runtime-card__hint">{{ choice.hint }}</span>
                <span v-if="!choice.installed" class="onboarding-runtime-card__size">
                  {{ t('onboarding.runtimeSizeHint', { size: choice.size }) }}
                </span>
              </button>
            </div>

            <div class="onboarding-runtime-mirror">
              <n-select
                v-model:value="runtimeMirror"
                size="small"
                :to="overlayRef || undefined"
                :options="[
                  { label: t('onboarding.runtimeMirrorAuto'), value: 'auto' },
                  { label: t('onboarding.runtimeMirrorUstc'), value: 'ustc' },
                  { label: t('onboarding.runtimeMirrorTsinghua'), value: 'tsinghua' },
                  { label: t('onboarding.runtimeMirrorAliyun'), value: 'aliyun' },
                  { label: t('onboarding.runtimeMirrorTencent'), value: 'tencent' },
                  { label: t('onboarding.runtimeMirrorPypi'), value: 'pypi' },
                ]"
              />
              <span class="onboarding-runtime-mirror__hint">{{ t('onboarding.runtimeMirrorHint') }}</span>
            </div>

            <p class="onboarding-hint">
              {{ t('onboarding.runtimeDriverHint') }}
              <template v-if="selectedBackendInstalled">
                <br />
                <span>{{ t('onboarding.runtimeAlreadyReady') }}</span>
              </template>
              <template v-else>
                <br />
                <span>{{ t('onboarding.runtimeSizeHint', { size: selectedBackendSize }) }}</span>
              </template>
            </p>
          </section>
        </div>

        <footer class="onboarding-content__foot">
          <n-button v-if="step > 0" quaternary size="large" @click="previousStep">
            {{ t('common.back') }}
          </n-button>
          <span class="onboarding-content__spacer" />
          <n-button
            v-if="step === totalSteps - 1"
            ref="completeButtonRef"
            type="primary"
            size="large"
            :disabled="!canGoNext"
            @click="finishOnboarding"
          >
            {{ t('onboarding.complete') }}
          </n-button>
          <n-button v-else type="primary" size="large" :disabled="!canGoNext" @click="nextStep">
            {{ step === totalSteps - 1 ? t('onboarding.complete') : t('common.next') }}
          </n-button>
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
  background:
    radial-gradient(circle at 12% 8%, color-mix(in srgb, var(--primary-glow) 30%, transparent), transparent 32%),
    radial-gradient(circle at 88% 92%, color-mix(in srgb, var(--primary-soft) 22%, transparent), transparent 30%),
    color-mix(in srgb, var(--surface) 90%, rgba(0, 0, 0, 0.7));
  backdrop-filter: blur(18px);
}

.onboarding-card {
  width: min(816px, 100%);
  display: grid;
  grid-template-columns: 272px 1fr;
  border-radius: 24px;
  border: 1px solid color-mix(in srgb, var(--primary-border) 24%, var(--outline));
  background: var(--surface-1);
  box-shadow: 0 28px 70px rgba(0, 0, 0, 0.26);
  overflow: hidden;
}

/* Left rail */
.onboarding-rail {
  display: flex;
  flex-direction: column;
  gap: 28px;
  padding: 28px 22px;
  border-right: 1px solid color-mix(in srgb, var(--outline) 80%, transparent);
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--primary-softer) 70%, transparent), transparent 46%),
    color-mix(in srgb, var(--surface-2) 50%, var(--surface-1));
}

.onboarding-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.onboarding-brand__text {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.onboarding-brand__text strong {
  font-size: 15px;
  letter-spacing: 0.01em;
}

.onboarding-brand__text span {
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.5;
}

.onboarding-nav {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 4px;
  position: relative;
}

.onboarding-nav__item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border-radius: 12px;
  color: var(--on-surface-muted);
  transition: color 180ms ease, background 180ms ease;
  min-width: 0;
}

.onboarding-nav__item.active {
  color: var(--on-surface);
  background: color-mix(in srgb, var(--primary-soft) 32%, transparent);
}

.onboarding-nav__item.done {
  color: var(--on-surface);
}

.onboarding-nav__marker {
  flex: 0 0 auto;
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: 9px;
  border: 1px solid color-mix(in srgb, var(--outline) 80%, transparent);
  background: color-mix(in srgb, var(--surface-1) 90%, transparent);
  color: var(--on-surface-muted);
  transition: 180ms ease;
}

.onboarding-nav__item.active .onboarding-nav__marker {
  color: var(--primary-strong);
  border-color: color-mix(in srgb, var(--primary-border) 80%, transparent);
  background: color-mix(in srgb, var(--primary-soft) 60%, var(--surface-2));
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--primary-glow) 18%, transparent);
}

.onboarding-nav__item.done .onboarding-nav__marker {
  color: var(--primary);
  border-color: color-mix(in srgb, var(--primary-border) 70%, transparent);
  background: color-mix(in srgb, var(--primary-softer) 80%, var(--surface-2));
}

.onboarding-nav__label {
  flex: 1 1 auto;
  font-size: 13px;
  font-weight: 600;
  min-width: 0;
  overflow: hidden;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-height: 1.35;
  white-space: normal;
}

/* Right content */
.onboarding-content {
  display: grid;
  grid-template-rows: auto 1fr auto;
  gap: 20px;
  padding: 32px 32px 24px;
  min-width: 0;
}

.onboarding-content__head {
  display: grid;
  gap: 6px;
}

.onboarding-content__counter {
  color: var(--primary-strong);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

.onboarding-content__head h1 {
  margin: 0;
  font-size: 24px;
  letter-spacing: 0.01em;
}

.onboarding-content__head p {
  margin: 0;
  color: var(--on-surface-muted);
  line-height: 1.6;
  font-size: 13px;
}

.onboarding-content__body {
  align-self: start;
}

.onboarding-content__foot {
  display: flex;
  align-items: center;
  gap: 10px;
}

.onboarding-content__spacer {
  flex: 1 1 auto;
}

.onboarding-section {
  display: grid;
  gap: 14px;
}

.onboarding-chip-grid {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.onboarding-chip {
  min-width: 0;
  border: 1px solid color-mix(in srgb, var(--outline) 76%, transparent);
  border-radius: 14px;
  padding: 14px 16px;
  color: var(--on-surface-muted);
  background: color-mix(in srgb, var(--surface-2) 74%, transparent);
  cursor: pointer;
  transition: 180ms ease;
}

.onboarding-chip:hover,
.onboarding-chip.active {
  color: var(--on-surface);
  border-color: color-mix(in srgb, var(--primary-border) 84%, transparent);
  background: color-mix(in srgb, var(--primary-soft) 34%, var(--surface-2));
}

.onboarding-accent-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  display: grid;
  gap: 10px;
}

.onboarding-accent-card {
  display: grid;
  gap: 8px;
  justify-items: flex-start;
  padding: 14px 16px;
  border-radius: 16px;
  border: 1px solid color-mix(in srgb, var(--outline) 76%, transparent);
  background: color-mix(in srgb, var(--surface-2) 68%, transparent);
  color: var(--on-surface);
  cursor: pointer;
  transition: 180ms ease;
}

.onboarding-accent-card.active,
.onboarding-accent-card:hover {
  border-color: color-mix(in srgb, var(--primary-border) 88%, transparent);
  background: color-mix(in srgb, var(--primary-soft) 22%, var(--surface-2));
  box-shadow: 0 10px 24px color-mix(in srgb, var(--primary-glow) 12%, transparent);
}

.onboarding-accent-card__swatches {
  display: inline-flex;
  gap: 8px;
}

.onboarding-accent-card__swatch {
  width: 24px;
  height: 24px;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, var(--outline) 70%, transparent);
}

.onboarding-accent-card__label {
  font-size: 13px;
  font-weight: 600;
}

.onboarding-path-card {
  display: grid;
  gap: 8px;
  padding: 16px;
  border-radius: 16px;
  border: 1px solid color-mix(in srgb, var(--outline) 72%, transparent);
  background: color-mix(in srgb, var(--surface-2) 56%, transparent);
}

.onboarding-path-card span,
.onboarding-hint {
  color: var(--on-surface-muted);
  font-size: 13px;
  line-height: 1.6;
}

.onboarding-path-card code {
  padding: 10px 12px;
  border-radius: 12px;
  border: 1px solid color-mix(in srgb, var(--outline) 72%, transparent);
  background: color-mix(in srgb, var(--surface-1) 92%, transparent);
  color: var(--on-surface);
  font-family: "JetBrains Mono", "Cascadia Code", Consolas, "MiSans", "PingFang SC", "Microsoft YaHei", ui-monospace, monospace;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.onboarding-path-card__actions {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.onboarding-path-card__actions .onboarding-hint {
  margin: 0;
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

.onboarding-content__body :deep(.n-base-selection) {
  border-radius: 12px;
}

@media (max-width: 720px) {
  .onboarding-overlay {
    padding: 16px;
  }

  .onboarding-card {
    grid-template-columns: minmax(0, 1fr);
  }

  .onboarding-rail {
    border-right: none;
    border-bottom: 1px solid color-mix(in srgb, var(--outline) 80%, transparent);
    padding: 20px;
    gap: 18px;
  }

  .onboarding-nav {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .onboarding-content {
    padding: 24px 20px 20px;
  }

  .onboarding-chip-grid,
  .onboarding-accent-grid,
  .onboarding-runtime-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .migration-summary-grid {
    grid-template-columns: 1fr;
  }

  .migration-dialog__status,
  .migration-progress-meta {
    flex-direction: column;
  }
}

.onboarding-runtime-section {
  gap: 14px;
}

.onboarding-runtime-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.onboarding-runtime-card {
  display: grid;
  gap: 6px;
  padding: 16px;
  border-radius: 16px;
  border: 1px solid color-mix(in srgb, var(--outline) 76%, transparent);
  background: color-mix(in srgb, var(--surface-2) 68%, transparent);
  color: var(--on-surface);
  cursor: pointer;
  text-align: left;
  transition: 180ms ease;
}

.onboarding-runtime-card.active,
.onboarding-runtime-card:hover {
  border-color: color-mix(in srgb, var(--primary-border) 88%, transparent);
  background: color-mix(in srgb, var(--primary-soft) 22%, var(--surface-2));
  box-shadow: 0 8px 20px color-mix(in srgb, var(--primary-glow) 10%, transparent);
}

.onboarding-runtime-card__title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.onboarding-runtime-card__label {
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.01em;
}

.onboarding-runtime-card__size {
  color: var(--on-surface-muted);
  font-size: 11px;
  opacity: 0.8;
}

.onboarding-runtime-card__hint {
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.55;
}

.onboarding-runtime-mirror {
  max-width: 260px;
  display: grid;
  gap: 6px;
}

.onboarding-runtime-mirror__hint {
  color: var(--on-surface-muted);
  font-size: 11px;
  line-height: 1.45;
}

.onboarding-runtime-locked {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 18px;
  border-radius: 16px;
  border: 1px solid color-mix(in srgb, var(--outline) 76%, transparent);
  background: color-mix(in srgb, var(--surface-2) 68%, transparent);
  color: var(--on-surface-muted);
}

.onboarding-runtime-locked__copy {
  display: grid;
  gap: 4px;
}

.onboarding-runtime-locked__copy strong {
  color: var(--on-surface);
  font-size: 14px;
  font-weight: 700;
}

.onboarding-runtime-locked__copy span {
  font-size: 12px;
  line-height: 1.55;
}
</style>
