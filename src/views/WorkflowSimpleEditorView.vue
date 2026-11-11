<script setup lang="ts">
import { createLogger } from '@/utils/logger'

import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { useI18n } from 'vue-i18n'
import { getCurrentWindow } from '@tauri-apps/api/window'
import { invoke } from '@tauri-apps/api/core'
import { storeToRefs } from 'pinia'
import WorkflowRunPanel from '@/components/workflow/WorkflowRunPanel.vue'
import WorkflowExecutionProgress from '@/components/workflow/WorkflowExecutionProgress.vue'
import WorkflowNodeBindings from '@/components/workflow/WorkflowNodeBindings.vue'
import { useWorkflowWorkbench } from '@/composables/useWorkflowWorkbench'
import WorkflowSimpleNodeEditor from '@/components/workflow/WorkflowSimpleNodeEditor.vue'
import WorkflowRevisionConflictModal from '@/components/workflow/WorkflowRevisionConflictModal.vue'
import { useModelStore } from '@/stores/model'
import { WorkflowRevisionConflictError, useWorkflowStore, type WorkflowEntry } from '@/stores/workflow'
import { resolveWorkflowDescription } from '@/workflows/metadata'
import {
  analyzeSimpleWorkflow,
  buildSimpleWorkflowDefinition,
  configuredStemsFor,
  createDefaultSimpleEditorUi,
  createStepDraft,
  isSimpleAudioOperation,
  simpleProcessingInputLimits,
  hydrateSimpleWorkflow,
  type SimpleDraft,
  type SimpleWorkflowReasonCode,
} from '@/utils/workflowSimple'
import {
  canConnectSimple,
  cleanupSimpleDraft,
  simpleEnsembleInputTarget,
  simpleStepInputTarget,
} from '@/utils/simpleWorkflowEditor'
import { isSimpleWorkflowDefinition } from '@/workflows/formats'
import { WorkflowEditorInUseError } from '@/utils/workflowEditorState'
import { useDiagnosticWindowState } from '@/features/diagnostics/windowState'

const studioLog = createLogger('ui.views.WorkflowSimpleEditorView')

const route = useRoute()
const router = useRouter()
const message = useMessage()
const { t, locale } = useI18n()
const workflow = useWorkflowStore()
const model = useModelStore()
const { workflows } = storeToRefs(workflow)
const { downloadedModels, selectedModel, modelInferenceOverrides } = storeToRefs(model)

const currentWindow = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window ? getCurrentWindow() : null
const isMacOS = typeof navigator !== 'undefined' && /Mac/i.test(navigator.platform)
const isStandaloneWindow = computed(() => Boolean(currentWindow && currentWindow.label !== 'main'))
const showCustomWindowChrome = computed(() => isStandaloneWindow.value && !isMacOS)
const isMaximized = ref(false)
const name = ref('')
const description = ref('')
const showOutputSettings = ref(false)
const draft = ref<SimpleDraft>(hydrateSimpleWorkflow({ steps: [] }))
const editingId = ref('')
const expectedUpdatedAt = ref<number | undefined>()
const loaded = ref(false)
const loadError = ref('')
const editorRenderKey = ref(0)
const saving = ref(false)
const saveError = ref('')
const nameEditing = ref(false)
const closing = ref(false)
const editorRef = ref<{ finishNameEdit: () => void } | null>(null)
let autosaveTimer: ReturnType<typeof setTimeout> | undefined
let saveQueue: Promise<void> = Promise.resolve()
let pendingSaves = 0
let saveGeneration = 0
let disposed = false
let runPromise: Promise<void> | null = null
let starterSnapshot = ''
const initialSnapshot = ref('')
const showClosePrompt = ref(false)
const showRevisionConflict = ref(false)
const pendingConflict = ref<{ definition: Record<string, unknown>; name: string; description: string } | null>(null)
let unlistenResize: (() => void) | undefined
let unlistenCloseRequested: (() => void) | undefined

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function snapshot() {
  return JSON.stringify({ name: name.value, description: description.value, draft: draft.value })
}

const dirty = computed(() => loaded.value && snapshot() !== initialSnapshot.value)

const workflowKey = computed(() => editingId.value || 'new-simple')
const currentDefinition = computed(() => buildSimpleWorkflowDefinition(draft.value) as unknown as Record<string, unknown>)
const workbench = useWorkflowWorkbench({ workflowKey, definition: () => currentDefinition.value, save: () => persist() })
const { selectedJobId, liveNodeStates, liveNodeLogs, executionMatchesDraft, submitting } = workbench
let confirmedClose = false
onBeforeRouteLeave(async () => {
  if (confirmedClose || !loaded.value) return true
  editorRef.value?.finishNameEdit()
  closing.value = true
  try { return await flushChanges() || window.confirm(t('workflows.simpleUnsavedHint')) }
  finally { closing.value = false }
})

const simpleReasonKeys: Record<SimpleWorkflowReasonCode, string> = {
  graph_workflow: 'workflows.simpleReasonGraphWorkflow',
  advanced_parameters: 'workflows.simpleReasonAdvancedParameters',
  invalid_definition: 'workflows.simpleReasonInvalidDefinition',
}

function blockUnsupportedEntry(reasonCodes: SimpleWorkflowReasonCode[]) {
  const reason = reasonCodes.map(code => t(simpleReasonKeys[code])).join(' ')
  loadError.value = t('workflows.simpleEditorBlockedHint', { reason })
  loaded.value = false
  initialSnapshot.value = ''
}

const formError = computed(() => {
  if (!name.value.trim()) return t('workflows.nameRequired')
  if (!draft.value.steps.length && !draft.value.ensembles.length) return t('workflows.stepsRequired')
  for (const [index, step] of draft.value.steps.entries()) {
    const stepLabel = t('workflows.stepTitle', { index: index + 1 })
    if (!step.model.trim()) return t('workflows.stepModelRequired', { id: stepLabel })
    if (!step.input.trim()) return t('workflows.stepInputRequired', { id: stepLabel })
    if (!canConnectSimple(draft.value, step.input, simpleStepInputTarget(step.id)).ok) return t('workflows.invalidConnection')
    if (!step.stems.length) return t('workflows.stepStemsRequired', { id: stepLabel })
  }
  for (const [index, ensemble] of draft.value.ensembles.entries()) {
    const audioOperation = isSimpleAudioOperation(ensemble.algorithm)
    const label = audioOperation
      ? `${t(`workflows.audioOperations.${ensemble.algorithm}.title`)} ${index + 1}`
      : t('workflows.ensembleTitle', { index: index + 1 })
    const limits = simpleProcessingInputLimits(ensemble.algorithm)
    const inputError = audioOperation
      ? t('workflows.audioOperationInputsRequired', { id: label, min: limits.min, max: limits.max })
      : t('workflows.ensembleInputsRequired', { id: label })
    if (!ensemble.outputStem.trim()) return t('workflows.ensembleStemRequired', { id: label })
    if (ensemble.inputs.length < limits.min || ensemble.inputs.length > limits.max) return inputError
    for (const [inputIndex, input] of ensemble.inputs.entries()) {
      if (!canConnectSimple(draft.value, input.source, simpleEnsembleInputTarget(ensemble.id, inputIndex)).ok) {
        return inputError
      }
      if (audioOperation && input.weight !== 1) return t('workflows.invalidConnection')
      if (!Number.isFinite(input.weight) || input.weight <= 0) return t('workflows.ensembleWeightInvalid', { id: label })
    }
  }
  const hasSavedStep = draft.value.steps.some(step => Object.keys(step.save || {}).length)
  const hasSavedEnsemble = draft.value.ensembles.some(ensemble => ensemble.save)
  if (!hasSavedStep && !hasSavedEnsemble) return t('workflows.workflowNoSaveOutputs')
  return ''
})
const modelAdvisory = computed(() => {
  const downloaded = new Set(downloadedModels.value.map(item => item.name))
  const missingIndex = draft.value.steps.findIndex(step => !downloaded.has(step.model.trim()))
  return missingIndex >= 0
    ? t('workflows.stepModelNotDownloaded', { id: t('workflows.stepTitle', { index: missingIndex + 1 }) })
    : ''
})
const canSave = computed(() => loaded.value && !loadError.value && Boolean(name.value.trim())
  && analyzeSimpleWorkflow(currentDefinition.value).editable)
const canRun = computed(() => !formError.value && !closing.value)

function hydrateStepModelTypes(value: SimpleDraft) {
  value.steps.forEach((step) => {
    if (step.modelType?.trim()) return
    const entry = downloadedModels.value.find(model => (
      model.name === step.model || model.aliases?.includes(step.model)
    ))
    if (entry?.modelType) step.modelType = entry.modelType
  })
}

function createExampleDraft(): SimpleDraft {
  const example = hydrateSimpleWorkflow({ steps: [] })
  const step = createStepDraft(0)
  const modelEntry = downloadedModels.value.find(item => item.name === selectedModel.value)
    || [...downloadedModels.value].sort((left, right) => left.name.localeCompare(
      right.name,
      locale.value === 'zh-CN' ? 'zh-CN' : 'en',
    ))[0]
  if (modelEntry) {
    step.model = modelEntry.name
    step.modelType = modelEntry.modelType || undefined
    step.stems = configuredStemsFor(modelEntry)
    step.outputNames = Object.fromEntries(step.stems.map(stem => [stem, '%filename%_%stem%_%model%']))
    step.save = Object.fromEntries(step.stems.map(stem => [stem, 'Default']))
  }
  example.steps = [step]
  example.ui = createDefaultSimpleEditorUi(example.steps)
  return example
}

function loadEntry(entry?: WorkflowEntry | null) {
  if (loaded.value) editorRenderKey.value += 1
  nameEditing.value = false
  saveGeneration += 1
  cancelAutosave()
  saveError.value = ''
  editingId.value = entry?.id || ''
  if (!loaded.value) {
    workbench.session.value.bindings.inputs = {}
    workbench.session.value.bindings.primaryInput = ''
  }
  name.value = entry?.name || t('workflows.newWorkflow')
  description.value = resolveWorkflowDescription(entry)
  loadError.value = ''
  if (entry) {
    const analysis = analyzeSimpleWorkflow(entry.definition)
    if (!analysis.editable) {
      blockUnsupportedEntry(analysis.reasonCodes)
      return
    }
  }
  const hydrated = entry && isSimpleWorkflowDefinition(entry.definition)
    ? hydrateSimpleWorkflow(entry.definition)
    : hydrateSimpleWorkflow({ steps: [] })
  hydrateStepModelTypes(hydrated)
  if (!entry) {
    const example = createExampleDraft()
    hydrated.defaultDevice = example.defaultDevice
    hydrated.defaultFormat = example.defaultFormat
    hydrated.defaultNormalize = example.defaultNormalize
    hydrated.steps = example.steps
    hydrated.ui = example.ui
  } else {
    hydrated.ui = hydrated.ui || createDefaultSimpleEditorUi(hydrated.steps, hydrated.ensembles)
  }
  cleanupSimpleDraft(hydrated)
  draft.value = clone(hydrated)
  expectedUpdatedAt.value = entry?.updatedAt
  initialSnapshot.value = snapshot()
  starterSnapshot = entry ? '' : snapshot()
  loaded.value = true
}

function cancelAutosave() {
  if (autosaveTimer !== undefined) clearTimeout(autosaveTimer)
  autosaveTimer = undefined
}

function scheduleAutosave() {
  cancelAutosave()
  if (disposed || closing.value || saving.value || submitting.value || nameEditing.value
    || showRevisionConflict.value || saveError.value || !canSave.value || (!dirty.value && editingId.value)) return
  autosaveTimer = setTimeout(() => { autosaveTimer = undefined; void persist() }, 600)
}

function persist(force = false): Promise<WorkflowEntry | null> {
  cancelAutosave()
  if (disposed || (!canSave.value && !force)) return Promise.resolve(null)
  let savedSnapshot: string
  let captured: { name: string; description: string; definition: Record<string, unknown> }
  try {
    cleanupSimpleDraft(draft.value)
    savedSnapshot = snapshot()
    captured = clone({
      name: name.value.trim(), description: description.value.trim(),
      definition: buildSimpleWorkflowDefinition(draft.value) as unknown as Record<string, unknown>,
    })
  } catch (error) {
    saveError.value = error instanceof Error ? error.message : String(error)
    return Promise.resolve(null)
  }
  const generation = saveGeneration
  pendingSaves += 1
  saving.value = true
  const request = saveQueue.then(async () => {
    if (disposed || generation !== saveGeneration || (showRevisionConflict.value && !force)) return null
    const existing = workflows.value.find(item => item.id === editingId.value)
    if (!force && existing && savedSnapshot === initialSnapshot.value && existing.updatedAt === expectedUpdatedAt.value
      && JSON.stringify(existing.definition) === JSON.stringify(captured.definition)) return existing
    try {
      const entry = await workflow.saveWorkflow({
        ...captured, id: editingId.value || undefined,
        expectedUpdatedAt: force ? undefined : expectedUpdatedAt.value, force,
      })
      if (disposed || generation !== saveGeneration) return null
      workbench.adoptWorkflowKey(entry.id)
      editingId.value = entry.id
      expectedUpdatedAt.value = entry.updatedAt
      initialSnapshot.value = savedSnapshot
      saveError.value = ''
      return entry
    } catch (error) {
      saveError.value = error instanceof WorkflowEditorInUseError ? t('workflows.editorInUseHint') : error instanceof Error ? error.message : String(error)
      if (error instanceof WorkflowRevisionConflictError) {
        pendingConflict.value = captured
        showRevisionConflict.value = true
      }
      return null
    }
  })
  saveQueue = request.then(() => undefined, () => undefined)
  return request.finally(() => {
    pendingSaves -= 1
    saving.value = pendingSaves > 0
    scheduleAutosave()
  })
}

async function flushChanges() {
  cancelAutosave()
  if (runPromise) await runPromise
  await saveQueue
  while (dirty.value || !editingId.value) {
    if (showRevisionConflict.value || !canSave.value || !await persist()) return false
  }
  return true
}

async function closeEditor() {
  if (closing.value) return
  editorRef.value?.finishNameEdit()
  closing.value = true
  try {
    if (loaded.value && !await flushChanges()) { if (!showRevisionConflict.value) showClosePrompt.value = true; return }
    await destroyWindow()
  } finally {
    closing.value = false
  }
}

async function destroyWindow() {
  if (isStandaloneWindow.value && currentWindow) {
    try { await invoke('close_current_window'); return } catch { await currentWindow.close().catch(() => undefined); return }
  }
  confirmedClose = true
  await router.push('/workflows')
}

async function refreshMaximized() {
  if (!isStandaloneWindow.value || !currentWindow) {
    isMaximized.value = false
    return
  }
  try {
    isMaximized.value = await invoke<boolean>('is_current_window_maximized')
  } catch {
    try { isMaximized.value = await currentWindow.isMaximized() } catch { isMaximized.value = false }
  }
}

function startWindowDrag(event?: MouseEvent) {
  if (event?.detail && event.detail > 1) {
    void toggleMaximizeWindow()
    return
  }
  if (!currentWindow) return
  invoke('start_drag_current_window').catch(() => currentWindow.startDragging().catch(() => undefined))
}

async function minimizeWindow() {
  if (!currentWindow) return
  try { await invoke('minimize_current_window') } catch { await currentWindow.minimize().catch(() => undefined) }
}

async function toggleMaximizeWindow() {
  if (!currentWindow) return
  try {
    isMaximized.value = await invoke<boolean>('toggle_maximize_current_window')
  } catch {
    await currentWindow.toggleMaximize().then(refreshMaximized).catch(() => undefined)
  }
}

async function saveAndClose() {
  // The save button in the close prompt must follow the same validation as
  // the editor toolbar.  Previously `persist()` returned null silently when
  // the draft was invalid, while the prompt was already dismissed, which
  // made the action appear to do nothing.
  if (saving.value) return
  if (!canSave.value) {
    message.warning(formError.value || t('workflows.simpleReasonInvalidDefinition'))
    return
  }
  showClosePrompt.value = false
  await closeEditor()
}

async function runWorkflow() {
  if (runPromise) return runPromise
  editorRef.value?.finishNameEdit()
  const request = workbench.run()
  runPromise = request
  try { await request }
  finally { if (runPromise === request) runPromise = null; scheduleAutosave() }
}

async function reloadConflict() {
  if (!editingId.value) return
  await workflow.reload()
  const latest = workflows.value.find(item => item.id === editingId.value)
  if (latest) loadEntry(latest)
  pendingConflict.value = null
  showRevisionConflict.value = false
}

function saveConflictCopy() {
  const pending = pendingConflict.value
  if (!pending) return
  saveGeneration += 1
  cancelAutosave()
  saveError.value = ''
  pendingConflict.value = null
  showRevisionConflict.value = false
  const copyName = t('workflows.copyName', { name: name.value.trim() })
  workbench.adoptWorkflowKey('new-simple')
  editingId.value = ''
  expectedUpdatedAt.value = undefined
  name.value = copyName
  void persist()
}

function overwriteConflict() {
  pendingConflict.value = null
  showRevisionConflict.value = false
  saveError.value = ''
  void persist(true)
}

onMounted(async () => {
  await Promise.allSettled([workflow.initialize(), model.initialize()])
  // The standalone window has its own Pinia instance.  Use the persisted
  // model cache immediately, then refresh it in the background when the
  // backend is available so newly downloaded models become selectable without
  // delaying the editor window.
  if (!model.modelsLoaded) {
    void model.loadModels().catch((error) => {
      studioLog.warn('Failed to refresh models for simple workflow editor', error)
    })
  }
  const id = String(route.query.workflowId || '').trim()
  const entry = id ? workflows.value.find(item => item.id === id) : null
  if (id && !entry) {
    editingId.value = id
    blockUnsupportedEntry(['invalid_definition'])
  } else {
    loadEntry(entry)
  }
  await refreshMaximized()
  if (!currentWindow || currentWindow.label === 'main') return
  try { unlistenResize = await currentWindow.onResized(refreshMaximized) } catch {}
  try {
    unlistenCloseRequested = await currentWindow.onCloseRequested((event) => {
      event.preventDefault()
      void closeEditor()
    })
  } catch {}
})

watch(() => draft.value.steps.length, () => {
  if (!draft.value.ui) draft.value.ui = createDefaultSimpleEditorUi(draft.value.steps)
})

watch(snapshot, () => {
  if (starterSnapshot && snapshot() !== starterSnapshot) starterSnapshot = ''
  if (!showRevisionConflict.value) saveError.value = ''
  scheduleAutosave()
}, { flush: 'post' })
watch([loaded, saving, submitting, nameEditing, showRevisionConflict], scheduleAutosave, { flush: 'post' })

watch(downloadedModels, () => {
  hydrateStepModelTypes(draft.value)
  // A new editor can mount before the model cache finishes loading. Fill the
  // starter step once the first downloaded model becomes available, without
  // replacing edits the user has already made in the blank draft.
  if (!starterSnapshot || !loaded.value || snapshot() !== starterSnapshot || draft.value.ensembles.length || draft.value.steps.some(step => step.model.trim())) return
  const example = createExampleDraft()
  draft.value.steps = example.steps
  draft.value.ui = example.ui
  if (!editingId.value) initialSnapshot.value = snapshot()
  starterSnapshot = ''
  // Recreate the child once so its undo baseline includes the asynchronously
  // loaded starter template rather than the initial blank draft.
  editorRenderKey.value += 1
})

onBeforeUnmount(() => {
  disposed = true
  saveGeneration += 1
  cancelAutosave()
  unlistenResize?.()
  unlistenCloseRequested?.()
})

useDiagnosticWindowState('workflow', () => ({
  kind: 'workflow', workflowId: editingId.value, updatedAt: expectedUpdatedAt.value,
  saving: saving.value, dirty: dirty.value, error: saveError.value || loadError.value,
  revisionConflict: showRevisionConflict.value, recoveryBackupAvailable: Boolean(workflow.recoveryBackupPath),
}))
</script>

<template>
  <div class="simple-editor-page" :class="{ 'simple-editor-page--custom-chrome': showCustomWindowChrome }">
    <header v-if="showCustomWindowChrome" class="simple-editor-chrome">
      <div class="simple-editor-chrome__drag" data-tauri-drag-region @mousedown.left="startWindowDrag">
        <strong>{{ t('app.name') }}</strong><span>·</span><span>{{ t('workflows.simpleCreator') }}</span>
      </div>
      <div class="simple-editor-chrome__actions">
        <button type="button" :aria-label="t('common.minimize')" @click="minimizeWindow">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M2 6h8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" /></svg>
        </button>
        <button type="button" :aria-label="t('common.maximize')" @click="toggleMaximizeWindow">
          <svg v-if="isMaximized" width="12" height="12" viewBox="0 0 12 12" fill="none"><rect x="1.5" y="3.5" width="7" height="7" rx="1" stroke="currentColor" stroke-width="1.2" /><path d="M3.5 3.5V2a.5.5 0 01.5-.5h6a.5.5 0 01.5.5v6a.5.5 0 01-.5.5h-1.5" stroke="currentColor" stroke-width="1.2" /></svg>
          <svg v-else width="12" height="12" viewBox="0 0 12 12" fill="none"><rect x="2" y="2" width="8" height="8" rx="1.5" stroke="currentColor" stroke-width="1.2" /></svg>
        </button>
        <button type="button" class="danger" :aria-label="t('common.close')" @click="closeEditor">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" /></svg>
        </button>
      </div>
    </header>
    <div v-if="loadError" class="simple-editor-page__blocked">
      <n-result status="warning" :title="t('workflows.simpleEditorBlockedTitle')" :description="loadError">
        <template #footer><n-button type="primary" @click="destroyWindow">{{ t('workflows.returnToWorkflows') }}</n-button></template>
      </n-result>
    </div>
    <WorkflowSimpleNodeEditor
      v-else-if="loaded" ref="editorRef" :key="editorRenderKey" :inert="closing"
      v-model:draft="draft" v-model:name="name"
      :runtime-controls="true" :node-states="liveNodeStates" :node-logs="liveNodeLogs" :execution-visible="executionMatchesDraft"
      :models="downloadedModels" :model-inference-overrides="modelInferenceOverrides"
      :form-error="formError" :advisory="modelAdvisory" :can-run="canRun"
      @close="closeEditor" @run="runWorkflow" @name-editing="nameEditing = $event" @output-settings="showOutputSettings = true">
      <template #save-status>
        <span v-if="saving" class="simple-save-status">{{ t('workflows.autoSaveSaving') }}</span>
        <n-button v-else-if="saveError" size="tiny" quaternary type="error" :title="saveError" @click="persist()">{{ t('workflows.autoSaveFailed') }} · {{ t('common.retry') }}</n-button>
        <span v-else class="simple-save-status" :title="dirty || !editingId ? formError : ''">{{ dirty || !editingId ? t('workflows.autoSavePending') : t('workflows.autoSaveSaved') }}</span>
      </template>
      <template #runtime-input><WorkflowNodeBindings :workflow-key="workflowKey" :definition="currentDefinition" mode="inputs" :compact="true" /></template>
      <template #execution-progress><WorkflowExecutionProgress :job="workbench.focusedJob.value" :task="workbench.focusedTask.value" /></template>
      <template #run-controls>
        <WorkflowRunPanel :workflow-key="workflowKey" v-model:job-id="selectedJobId" size="small" :run-primary="true"
          :submitting="submitting" :can-run="canRun" :show-status="false"
          @run="runWorkflow" @cancel="workbench.cancel">

        </WorkflowRunPanel>
      </template>
    </WorkflowSimpleNodeEditor>
    <n-modal v-model:show="showOutputSettings" preset="card" :title="t('separate.output')" class="simple-output-settings-modal" style="width: min(480px, calc(100vw - 32px))">
      <WorkflowNodeBindings :workflow-key="workflowKey" :definition="currentDefinition" mode="outputs" :aggregate-output="true" />
      <template #footer><div class="simple-editor-page__prompt-actions"><n-button size="small" secondary @click="showOutputSettings = false">{{ t('common.close') }}</n-button></div></template>
    </n-modal>
    <n-modal v-model:show="showClosePrompt" preset="card" :title="t('workflows.simpleUnsavedTitle')" style="width: min(440px, calc(100vw - 32px))">
      <p>{{ saveError || formError || t('workflows.simpleUnsavedHint') }}</p>
      <template #footer><div class="simple-editor-page__prompt-actions"><n-button secondary @click="showClosePrompt = false; void destroyWindow()">{{ t('workflows.simpleDiscardChanges') }}</n-button><n-button type="primary" :loading="saving" :disabled="saving" @click="void saveAndClose()">{{ t('workflows.simpleSaveAndClose') }}</n-button></div></template>
    </n-modal>
    <WorkflowRevisionConflictModal
      v-model:show="showRevisionConflict"
      :workflow-name="name"
      @reload="reloadConflict"
      @save-copy="saveConflictCopy"
      @overwrite="overwriteConflict"
    />
  </div>
</template>

<style scoped>
.simple-editor-page { height: 100%; min-height: 0; background: var(--surface); color: var(--on-surface); -webkit-app-region: no-drag; }
.simple-editor-page * { -webkit-app-region: no-drag; }
.simple-editor-chrome { height: 40px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid color-mix(in srgb, var(--outline) 54%, transparent); background: color-mix(in srgb, var(--surface) 84%, transparent); backdrop-filter: blur(18px) saturate(1.15); user-select: none; }
.simple-editor-chrome__drag { display: flex; align-items: center; gap: 6px; height: 100%; padding-left: 12px; flex: 1; -webkit-app-region: drag; font-size: 12px; }
.simple-editor-chrome__drag span { color: var(--on-surface-muted); }
.simple-editor-chrome__actions { display: flex; height: 100%; }
.simple-editor-chrome__actions button { width: 46px; height: 100%; display: grid; place-items: center; border: 0; background: transparent; color: var(--on-surface-muted); cursor: pointer; transition: background 140ms ease, color 140ms ease; }
.simple-editor-chrome__actions button:hover { background: var(--surface-2); color: var(--on-surface); }
.simple-editor-chrome__actions button.danger:hover { background: var(--danger); color: #fff; }
.simple-editor-page__prompt-actions { display: flex; justify-content: flex-end; gap: 8px; }
.simple-save-status { flex: 0 0 auto; color: var(--on-surface-muted); font-size: 11px; white-space: nowrap; }
.simple-editor-page__blocked { height: 100%; display: grid; place-items: center; padding: 32px; }
.simple-editor-page--custom-chrome > .simple-editor-page__blocked { height: calc(100% - 40px); }
.simple-editor-page > :deep(.simple-node-editor) { height: 100%; }
.simple-editor-page--custom-chrome > :deep(.simple-node-editor) { height: calc(100% - 40px); }
</style>
