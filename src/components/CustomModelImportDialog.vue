<script setup lang="ts">
import { safeModelName } from '@/utils/modelName'
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { invoke } from '@tauri-apps/api/core'
import { DocumentAttachOutline, FolderOpenOutline } from '@vicons/ionicons5'
import { useModelStore, type CustomModelInspection } from '@/stores/model'
import { formatBytes } from '@/utils/format'

const props = defineProps<{ show: boolean }>()
const emit = defineEmits<{
  (e: 'update:show', value: boolean): void
  (e: 'imported', name: string): void
}>()

const { t } = useI18n()
const message = useMessage()
const modelStore = useModelStore()

const STEP_FILE = 0
const STEP_TYPE = 1
const STEP_DETAILS = 2
const STEP_IMPORT = 3

const step = ref(STEP_FILE)
const inspecting = ref(false)
const inspection = ref<CustomModelInspection | null>(null)
const inspectError = ref('')
const inspectionRunId = ref(0)

const modelPath = ref('')
const configPath = ref('')
const modelType = ref('')
const name = ref('')
const aliasText = ref('')
const importMode = ref<'reference' | 'copy'>('reference')
const verify = ref(true)
const force = ref(false)

const importState = computed(() => modelStore.customImportState)
const importing = computed(() => importState.value.status === 'importing')

const modelTypeOptions = computed(() => {
  const known = inspection.value?.knownModelTypes || []
  const suggested = new Set((inspection.value?.suggestions || []).map((item) => item.modelType))
  return known.map((value) => ({
    label: suggested.has(value) ? `${value} · ${t('models.customSuggestedTag')}` : value,
    value,
  }))
})

const nameError = computed(() => {
  const value = name.value.trim()
  if (!value) return t('models.customNameRequired')
  // pymss rejects any whitespace outright, so say so before the user submits.
  if (/\s/.test(value)) return t('models.customNameNoSpace')
  if (!safeModelName(value)) return t('models.customNamePathUnsafe')
  return ''
})

const aliases = computed(() => aliasText.value.split(',').map((item) => item.trim()).filter(Boolean))

// pymss validates aliases exactly like the name, so catch the same mistake here rather than
// letting it surface as a backend error after the file has already been copied.
const aliasError = computed(() =>
  aliases.value.some((alias) => /\s/.test(alias)) ? t('models.customAliasNoSpace') : '')

const canLeaveFileStep = computed(() => Boolean(modelPath.value && configPath.value))
const canLeaveTypeStep = computed(() => Boolean(modelType.value) && !inspecting.value && !inspectError.value)
const canLeaveDetailStep = computed(() =>
  !nameError.value && !aliasError.value)

function reset() {
  step.value = STEP_FILE
  inspectionRunId.value += 1
  inspecting.value = false
  inspection.value = null
  inspectError.value = ''
  modelPath.value = ''
  configPath.value = ''
  modelType.value = ''
  name.value = ''
  aliasText.value = ''
  importMode.value = 'reference'
  verify.value = true
  force.value = false
  modelStore.resetCustomImportState()
}

function clearInspectionForFileChange() {
  inspectionRunId.value += 1
  inspecting.value = false
  inspection.value = null
  inspectError.value = ''
  modelType.value = ''
}

watch(() => props.show, (visible) => {
  if (visible) reset()
})

async function pickWeights() {
  const picked = await invoke<string | null>('pick_model_weights_file', {
    title: t('models.customPickWeights'),
  })
  if (!picked) return
  modelPath.value = picked
  clearInspectionForFileChange()
}

async function pickConfig() {
  const picked = await invoke<string | null>('pick_model_config_file', {
    title: t('models.customPickConfig'),
  })
  if (!picked) return
  configPath.value = picked
  clearInspectionForFileChange()
}

async function runInspection() {
  if (!modelPath.value) return
  const runId = inspectionRunId.value + 1
  const inspectedModelPath = modelPath.value
  const inspectedConfigPath = configPath.value || null
  inspectionRunId.value = runId
  inspecting.value = true
  inspectError.value = ''
  try {
    const result = await modelStore.inspectCustomModel(inspectedModelPath, inspectedConfigPath)
    if (runId !== inspectionRunId.value || inspectedModelPath !== modelPath.value || inspectedConfigPath !== (configPath.value || null)) return
    inspection.value = result
    // Only prefill; never overwrite something the user already typed or chose.
    if (!name.value) name.value = result.suggestedName
    if (!modelType.value && result.suggestedModelType) modelType.value = result.suggestedModelType
    if (!configPath.value && result.configPath) configPath.value = result.configPath
  } catch (err) {
    if (runId !== inspectionRunId.value) return
    inspectError.value = err instanceof Error ? err.message : String(err)
    inspection.value = null
  } finally {
    if (runId === inspectionRunId.value) inspecting.value = false
  }
}

function suggestionBasis(basisCode: string, basisDetail: string) {
  if (basisCode === 'config_model_key') return t('models.customBasisConfigKey', { key: basisDetail })
  if (basisCode === 'config_kwargs_section') return t('models.customBasisConfigKwargs')
  if (basisCode === 'state_dict_key') return t('models.customBasisStateDictKey', { key: basisDetail })
  return basisDetail
}

function applySuggestion(value: string) {
  modelType.value = value
}

const cancelling = ref(false)

async function cancelImport() {
  cancelling.value = true
  try {
    await modelStore.cancelCustomModelImport()
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    cancelling.value = false
  }
}

async function startImport() {
  cancelling.value = false
  step.value = STEP_IMPORT
  try {
    await modelStore.importCustomModel({
      name: name.value.trim(),
      modelType: modelType.value,
      modelPath: modelPath.value,
      configPath: configPath.value || null,
      aliases: aliases.value,
      importMode: importMode.value,
      verify: verify.value,
      force: force.value,
    })
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

watch(() => importState.value.status, (status) => {
  if (status === 'success') {
    message.success(t('models.customImportSuccess', { name: importState.value.name }))
    emit('imported', importState.value.name)
    emit('update:show', false)
  }
})

async function goNext() {
  if (step.value === STEP_FILE && canLeaveFileStep.value) {
    step.value = STEP_TYPE
    await runInspection()
  }
  else if (step.value === STEP_TYPE && canLeaveTypeStep.value) step.value = STEP_DETAILS
  else if (step.value === STEP_DETAILS && canLeaveDetailStep.value) void startImport()
}

function goBack() {
  if (step.value > STEP_FILE) step.value -= 1
}

const nextDisabled = computed(() => {
  if (step.value === STEP_FILE) return !canLeaveFileStep.value
  if (step.value === STEP_TYPE) return !canLeaveTypeStep.value
  if (step.value === STEP_DETAILS) return !canLeaveDetailStep.value
  return true
})
</script>

<template>
  <!-- Dismissal is blocked while importing: reopening resets the wizard, which would orphan the
       running task and leave its progress with nowhere to go. -->
  <n-modal
    :show="show"
    :mask-closable="!importing"
    :close-on-esc="!importing"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <n-card
      class="cmi-modal"
      :bordered="false"
      :closable="!importing"
      role="dialog"
      aria-modal="true"
      @close="emit('update:show', false)"
    >
      <template #header>
        <span class="cmi-title">{{ t('models.customImportTitle') }}</span>
      </template>

      <n-steps :current="step + 1" size="small" class="cmi-steps">
        <n-step :title="t('models.customStepFile')" />
        <n-step :title="t('models.customStepType')" />
        <n-step :title="t('models.customStepDetails')" />
        <n-step :title="t('models.customStepImport')" />
      </n-steps>

      <!-- Step 1: weights file -->
      <section v-if="step === STEP_FILE" class="cmi-section">
        <p class="cmi-hint">{{ t('models.customFileHint') }}</p>
        <div class="cmi-file-row">
          <n-input v-model:value="modelPath" :placeholder="t('models.customFilePlaceholder')" readonly />
          <n-button secondary @click="pickWeights">
            <template #icon><n-icon :component="FolderOpenOutline" /></template>
            {{ t('models.customBrowse') }}
          </n-button>
        </div>
        <div class="cmi-file-row">
          <n-input v-model:value="configPath" :placeholder="t('models.customConfigPlaceholder')" readonly />
          <n-button secondary @click="pickConfig">
            <template #icon><n-icon :component="DocumentAttachOutline" /></template>
            {{ t('models.customBrowse') }}
          </n-button>
        </div>
      </section>

      <!-- Step 2: architecture -->
      <section v-else-if="step === STEP_TYPE" class="cmi-section">
        <p class="cmi-hint">{{ t('models.customTypeHint') }}</p>
        <n-spin v-if="inspecting" size="small" class="cmi-spin">
          <template #description>{{ t('models.customInspecting') }}</template>
        </n-spin>
        <n-alert v-else-if="inspectError" type="error" :bordered="false" class="cmi-alert">
          {{ inspectError }}
        </n-alert>
        <template v-else>
          <div v-if="inspection" class="cmi-facts">
            <div class="cmi-fact">
              <span>{{ t('models.customFactSize') }}</span>
              <strong>{{ formatBytes(inspection.sizeBytes) }}</strong>
            </div>
            <div v-if="inspection.instruments.length" class="cmi-fact">
              <span>{{ t('models.customFactStems') }}</span>
              <strong>{{ inspection.instruments.join(' / ') }}</strong>
            </div>
            <n-alert v-if="!inspection.stateDictReadable" type="warning" :bordered="false" class="cmi-alert">
              {{ t('models.customWeightsUnreadable') }}
            </n-alert>
          </div>
          <div v-if="inspection?.suggestions?.length" class="cmi-suggestions">
            <button
              v-for="item in inspection.suggestions"
              :key="item.modelType"
              type="button"
              class="cmi-suggestion"
              :class="{ 'cmi-suggestion--active': modelType === item.modelType }"
              @click="applySuggestion(item.modelType)"
            >
              <span class="cmi-suggestion__type">{{ item.modelType }}</span>
              <span class="cmi-suggestion__basis">{{ suggestionBasis(item.basisCode, item.basisDetail) }}</span>
            </button>
          </div>
          <n-alert v-else type="info" :bordered="false" class="cmi-alert">
            {{ t('models.customNoSuggestion') }}
          </n-alert>
          <div class="cmi-field">
            <label>{{ t('models.customTypeLabel') }}</label>
            <n-select
              v-model:value="modelType"
              :options="modelTypeOptions"
              to="body"
              filterable
              :placeholder="t('models.customTypePlaceholder')"
            />
          </div>
          <p class="cmi-hint cmi-hint--muted">{{ t('models.customTypeVerifyNote') }}</p>
        </template>
      </section>

      <!-- Step 3: name / config / import mode -->
      <section v-else-if="step === STEP_DETAILS" class="cmi-section">
        <div class="cmi-field">
          <label>{{ t('models.customNameLabel') }}</label>
          <n-input v-model:value="name" :status="nameError ? 'error' : undefined" :placeholder="t('models.customNamePlaceholder')" />
          <span v-if="nameError" class="cmi-error">{{ nameError }}</span>
        </div>
        <div class="cmi-field">
          <label>{{ t('models.customAliasLabel') }}</label>
          <n-input
            v-model:value="aliasText"
            :status="aliasError ? 'error' : undefined"
            :placeholder="t('models.customAliasPlaceholder')"
          />
          <span v-if="aliasError" class="cmi-error">{{ aliasError }}</span>
        </div>
        <div class="cmi-field">
          <label>{{ t('models.customImportModeLabel') }}</label>
          <n-radio-group v-model:value="importMode">
            <n-space vertical size="small">
              <n-radio value="reference">
                <span class="cmi-radio-title">{{ t('models.customModeReference') }}</span>
                <span class="cmi-radio-desc">{{ t('models.customModeReferenceDesc') }}</span>
              </n-radio>
              <n-radio value="copy">
                <span class="cmi-radio-title">{{ t('models.customModeCopy') }}</span>
                <span class="cmi-radio-desc">{{ t('models.customModeCopyDesc') }}</span>
              </n-radio>
            </n-space>
          </n-radio-group>
        </div>
        <div class="cmi-toggle-row">
          <n-switch v-model:value="verify" size="small" />
          <div>
            <span class="cmi-radio-title">{{ t('models.customVerifyLabel') }}</span>
            <span class="cmi-radio-desc">{{ t('models.customVerifyDesc') }}</span>
          </div>
        </div>
        <div class="cmi-toggle-row">
          <n-switch v-model:value="force" size="small" />
          <div>
            <span class="cmi-radio-title">{{ t('models.customForceLabel') }}</span>
            <span class="cmi-radio-desc">{{ t('models.customForceDesc') }}</span>
          </div>
        </div>
      </section>

      <!-- Step 4: progress -->
      <section v-else class="cmi-section">
        <n-progress
          type="line"
          :percentage="importState.progress"
          :status="importState.status === 'error' ? 'error' : 'default'"
          :processing="importing"
        />
        <p class="cmi-hint">
          {{ importState.stage === 'copying' ? t('models.customStageCopying')
            : importState.stage === 'registering' ? t('models.customStageRegistering')
            : importState.stage === 'verifying' ? t('models.customStageVerifying')
            : t('models.customStageStarting') }}
          <span v-if="importState.message" class="cmi-stage-detail">{{ importState.message }}</span>
        </p>
        <n-alert v-if="importState.status === 'error'" type="error" :bordered="false" class="cmi-alert">
          {{ importState.message }}
        </n-alert>
        <n-alert v-else-if="importState.status === 'cancelled'" type="warning" :bordered="false" class="cmi-alert">
          {{ t('models.customImportCancelled') }}
        </n-alert>
        <p v-if="importing && verify" class="cmi-hint cmi-hint--muted">{{ t('models.customVerifyWait') }}</p>
      </section>

      <template #footer>
        <div class="cmi-footer">
          <!-- One back button, never two: on the final step after a failure it also has to be
               offered, and both conditions used to be true at once. -->
          <n-button v-if="step > STEP_FILE && !importing" secondary @click="goBack">
            {{ t('models.customBack') }}
          </n-button>
          <div class="cmi-footer__spacer" />
          <!-- The only way out while importing: every other dismissal is blocked, and verifying a
               large model can take a while. -->
          <n-button v-if="importing" secondary :loading="cancelling" @click="cancelImport">
            {{ t('common.cancel') }}
          </n-button>
          <n-button v-if="!importing && step !== STEP_IMPORT" type="primary" :disabled="nextDisabled" @click="goNext">
            {{ step === STEP_DETAILS ? t('models.customStartImport') : t('models.customNext') }}
          </n-button>
        </div>
      </template>
    </n-card>
  </n-modal>
</template>

<style scoped>
.cmi-modal {
  width: var(--modal-width-sm);
}

.cmi-title {
  font-weight: 600;
}

.cmi-steps {
  margin-bottom: 20px;
}

.cmi-section {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-height: 220px;
}

.cmi-hint {
  margin: 0;
  font-size: 13px;
  color: var(--text-secondary, #888);
}

.cmi-hint--muted {
  font-size: 12px;
  opacity: 0.8;
}

.cmi-file-row {
  display: flex;
  gap: 8px;
}

.cmi-file-row :deep(.n-input) {
  flex: 1;
}

.cmi-spin {
  align-self: flex-start;
}

.cmi-alert {
  border-radius: 8px;
}

.cmi-facts {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.cmi-fact {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  padding: 8px 12px;
  border-radius: 8px;
  background: var(--fill-2, rgba(128, 128, 128, 0.08));
}

.cmi-suggestions {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.cmi-suggestion {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--border-color, rgba(128, 128, 128, 0.25));
  background: transparent;
  cursor: pointer;
  text-align: left;
  transition: border-color 0.15s ease, background 0.15s ease;
}

.cmi-suggestion:hover {
  background: var(--fill-2, rgba(128, 128, 128, 0.08));
}

.cmi-suggestion--active {
  border-color: var(--primary-color, #63e2b7);
  background: var(--fill-2, rgba(128, 128, 128, 0.08));
}

.cmi-suggestion__type {
  font-weight: 600;
  font-size: 14px;
}

.cmi-suggestion__basis {
  font-size: 12px;
  color: var(--text-secondary, #888);
}

.cmi-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.cmi-field > label {
  font-size: 13px;
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 6px;
}

.cmi-required {
  font-size: 11px;
  color: var(--error-color, #e88080);
}

.cmi-optional {
  font-size: 11px;
  color: var(--text-secondary, #888);
}

.cmi-error {
  font-size: 12px;
  color: var(--error-color, #e88080);
}

.cmi-radio-title {
  display: block;
  font-size: 13px;
  font-weight: 500;
}

.cmi-radio-desc {
  display: block;
  font-size: 12px;
  color: var(--text-secondary, #888);
}

.cmi-toggle-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.cmi-stage-detail {
  display: block;
  font-size: 12px;
  opacity: 0.75;
  word-break: break-all;
}

.cmi-footer {
  display: flex;
  gap: 8px;
  align-items: center;
}

.cmi-footer__spacer {
  flex: 1;
}
</style>
