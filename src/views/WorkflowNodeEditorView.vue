<script setup lang="ts">
import { createLogger } from '@/utils/logger'

import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useMessage, type InputInst } from 'naive-ui'
import { FolderOpenOutline } from '@vicons/ionicons5'
import { getCurrentWindow } from '@tauri-apps/api/window'
import { invoke } from '@tauri-apps/api/core'
import { storeToRefs } from 'pinia'
import WorkflowRunPanel from '@/components/workflow/WorkflowRunPanel.vue'
import WorkflowExecutionProgress from '@/components/workflow/WorkflowExecutionProgress.vue'
import WorkflowNodeBindings from '@/components/workflow/WorkflowNodeBindings.vue'
import { WorkflowEditorInUseError } from '@/utils/workflowEditorState'
import { useWorkflowWorkbench } from '@/composables/useWorkflowWorkbench'
import { cloneWorkflow } from '@/workflows/workbench'
import WorkflowNodeEditor from '@/components/workflow/WorkflowNodeEditorLite.vue'
import WorkflowRevisionConflictModal from '@/components/workflow/WorkflowRevisionConflictModal.vue'
import { useModelStore } from '@/stores/model'
import { useWorkflowNodeStore } from '@/stores/workflowNodes'
import {
  useWorkflowStore,
  WorkflowRevisionConflictError,
  type WorkflowEntry,
} from '@/stores/workflow'
import {
  isGraphWorkflowDefinition,
  isWorkflowSaveNodeType,
  isWorkflowSeparationNodeType,
} from '@/workflows/formats'
import { storeGraphDefaults } from '@/workflows/graphDefaults'

const studioLog = createLogger('ui.views.WorkflowNodeEditorView')

const { t, locale } = useI18n()
const route = useRoute()
const router = useRouter()
const message = useMessage()
const workflow = useWorkflowStore()
const model = useModelStore()
const workflowNodes = useWorkflowNodeStore()
void workflowNodes.load().catch(() => undefined)
const { workflows } = storeToRefs(workflow)
const { models, downloadedModels } = storeToRefs(model)

const editingId = ref('')
const name = ref('')
const description = ref('')
const defaultDevice = ref('auto')
const defaultFormat = ref('wav')
const definition = ref<Record<string, unknown>>({})
const loaded = ref(false)
const editingName = ref(false)
const nameBeforeEdit = ref('')
const nameInputRef = ref<InputInst | null>(null)
const editorRef = ref<{ snapshotDefinition: () => Record<string, unknown> } | null>(null)
const loadedUpdatedAt = ref<number | undefined>()
const editorKey = ref(0)
const saving = ref(false)
const saveError = ref('')
const closing = ref(false)
let autosaveTimer: ReturnType<typeof setTimeout> | undefined
let saveQueue: Promise<void> = Promise.resolve()
let pendingSaves = 0
let saveGeneration = 0
let disposed = false
const initialSnapshot = ref('')
const showClosePrompt = ref(false)
const showRevisionConflict = ref(false)
const pendingDefinition = ref<Record<string, unknown> | null>(null)
const formatError = ref('')
const canvasError = ref('')
const showOutputSettings = ref(false)

const hasTauriWindow = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
const currentWindow = hasTauriWindow ? getCurrentWindow() : null
const isStandaloneWindow = computed(() => Boolean(currentWindow && currentWindow.label !== 'main'))
const isMacOS = typeof navigator !== 'undefined' && /Mac/i.test(navigator.platform)
const showCustomWindowChrome = computed(() => isStandaloneWindow.value && !isMacOS)
const isMaximized = ref(false)

const modelOptions = computed(() => [...downloadedModels.value]
  .sort((a, b) => a.name.localeCompare(b.name, locale.value === 'zh-CN' ? 'zh-CN' : 'en'))
  .map(item => ({
    label: item.name,
    value: item.name,
  })))

const formError = computed(() => {
  if (formatError.value) return formatError.value
  if (canvasError.value) return canvasError.value
  if (!name.value.trim()) return t('workflows.nameRequired')
  const nodes = Array.isArray(definition.value.nodes) ? definition.value.nodes as any[] : []
  if (!nodes.length) return t('workflows.stepsRequired')
  // Deep validation belongs to pymss. Incomplete graphs can still be saved
  // as drafts; these checks only prevent submitting an incomplete workflow.
  const hasOutput = nodes.some(n => isWorkflowSaveNodeType(n.type, workflowNodes.saveNodeTypes))
  if (!hasOutput) {
    if (workflowNodes.loading) return t('common.loading')
    if (workflowNodes.error) return t('workflows.nodeMetadataUnavailable', { error: workflowNodes.error })
    return t('workflows.workflowNoSaveOutputs')
  }
  return ''
})

/** Advisory (non-blocking): separate nodes referencing models that are not in
 * the downloaded list. Saving a graph must never be blocked by model state —
 * the model may be downloaded later, or the graph shared to another machine.
 * comfy-mss serializes model widgets as ``[category] filename`` — strip the
 * annotation prefix before matching (mirrors pymss' runtime behaviour). */
const missingModelNodes = computed(() => {
  const nodes = Array.isArray(definition.value.nodes) ? definition.value.nodes as any[] : []
  const downloaded = new Set(downloadedModels.value.map(item => item.name))
  const missing: string[] = []
  for (const n of nodes) {
    if (isWorkflowSeparationNodeType(n.type)) {
      const raw = String((n as any).widgets_values?.[0] || '').trim()
      const model = raw.replace(/^\[[^\]]*\]\s*/, '').trim()
      if (model && !downloaded.has(model)) missing.push(String(n.id))
    }
  }
  return missing
})
const canSave = computed(() => loaded.value && canvasInitialized.value
  && !formatError.value && !canvasError.value && Boolean(name.value.trim()))

function snapshot() {
  return JSON.stringify({
    name: name.value,
    description: description.value,
    defaultDevice: defaultDevice.value,
    defaultFormat: defaultFormat.value,
    definition: definition.value,
  })
}

const dirty = computed(() => loaded.value
  && Boolean(initialSnapshot.value)
  && snapshot() !== initialSnapshot.value)

const workflowKey = computed(() => editingId.value || 'new-graph')
const workbench = useWorkflowWorkbench({ workflowKey, definition: () => definition.value, save: () => save(undefined, { silent: true }) })
const { selectedJobId, submitting } = workbench
const runningNodeIds = computed(() => workbench.running.value
  ? Object.entries(workbench.liveNodeStates.value).filter(([, state]) => state.status === 'running').map(([id]) => id)
  : [])
let runPromise: Promise<void> | null = null
async function runWorkflow() {
  if (runPromise) return runPromise
  finishNameEdit()
  syncCurrentDefinition()
  const operation = workbench.run()
  runPromise = operation
  try { await operation }
  finally { if (runPromise === operation) runPromise = null; scheduleAutosave() }
}
const canRun = computed(() => !formError.value && !closing.value)
let confirmedClose = false
onBeforeRouteLeave(async () => {
  if (confirmedClose || !loaded.value) return true
  finishNameEdit()
  closing.value = true
  try { return await flushChanges() || window.confirm(t('workflows.simpleUnsavedHint')) }
  finally { closing.value = false }
})

const canvasInitialized = ref(false)
function initializeEditorBaseline() {
  if (!canvasInitialized.value) captureInitialSnapshot()
  canvasInitialized.value = true
  scheduleAutosave()
}
function captureInitialSnapshot() {
  initialSnapshot.value = snapshot()
}

function syncCurrentDefinition() {
  const current = editorRef.value?.snapshotDefinition()
  if (current) definition.value = current
}

function createFreshDefinition(): Record<string, unknown> {
  // An empty comfy-mss graph; the editor seeds a starter workflow on mount.
  return { last_node_id: 0, last_link_id: 0, nodes: [], links: [], version: 1 }
}

function restoreDefaultControls(defaults: { device: string; outputFormat: string }) {
  defaultDevice.value = defaults.device
  defaultFormat.value = defaults.outputFormat
}

function loadWorkflow(item?: WorkflowEntry | null) {
  saveGeneration += 1
  cancelAutosave()
  saveError.value = ''
  canvasInitialized.value = false
  canvasError.value = ''
  if (!item) {
    formatError.value = ''
    editingId.value = ''
    name.value = t('workflows.newWorkflow')
    description.value = ''
    defaultDevice.value = 'auto'
    defaultFormat.value = 'wav'
    definition.value = createFreshDefinition()
    loadedUpdatedAt.value = undefined
    return
  }
  editingId.value = item.id
  name.value = item.name
  description.value = item.description
  if (!isGraphWorkflowDefinition(item.definition)) {
    formatError.value = t('workflows.graphEditorFormatRequired')
    defaultDevice.value = 'auto'
    defaultFormat.value = 'wav'
    definition.value = createFreshDefinition()
    loadedUpdatedAt.value = item.updatedAt
    return
  }
  formatError.value = ''
  definition.value = item.definition && typeof item.definition === 'object' && Object.keys(item.definition).length
    ? JSON.parse(JSON.stringify(item.definition)) as Record<string, unknown>
    : createFreshDefinition()
  loadedUpdatedAt.value = item.updatedAt
  // Read defaults back from the graph's extra.appDefaults if present.
  const extra = (definition.value.extra as Record<string, unknown> | undefined) || {}
  const appDefaults = (extra.appDefaults as Record<string, unknown> | undefined) || {}
  defaultDevice.value = String(appDefaults.device || 'auto')
  defaultFormat.value = String(appDefaults.output_format || 'wav')
  workflow.selectWorkflow(item.id)
}

function cancelAutosave() {
  if (autosaveTimer !== undefined) clearTimeout(autosaveTimer)
  autosaveTimer = undefined
}

function scheduleAutosave() {
  cancelAutosave()
  if (disposed || closing.value || saving.value || submitting.value || editingName.value
    || showClosePrompt.value || showRevisionConflict.value || saveError.value
    || !canSave.value || (!dirty.value && editingId.value)) return
  autosaveTimer = setTimeout(() => { autosaveTimer = undefined; void save(undefined, { silent: true }) }, 600)
}

function persistDefinition(
  definitionToSave: Record<string, unknown>,
  options: { force?: boolean; silent?: boolean } = {},
): Promise<WorkflowEntry | null> {
  cancelAutosave()
  if (disposed || !canSave.value) return Promise.resolve(null)
  let savedSnapshot: string
  let captured: { name: string; description: string; definition: Record<string, unknown> }
  try {
    savedSnapshot = snapshot()
    captured = cloneWorkflow({ name: name.value.trim(), description: description.value.trim(), definition: definitionToSave })
  } catch (error) {
    saveError.value = error instanceof Error ? error.message : String(error)
    return Promise.resolve(null)
  }
  const generation = saveGeneration
  pendingSaves += 1
  saving.value = true
  const request = saveQueue.then(async () => {
    if (disposed || generation !== saveGeneration || (showRevisionConflict.value && !options.force)) return null
    const existing = workflows.value.find(item => item.id === editingId.value)
    if (!options.force && existing && savedSnapshot === initialSnapshot.value && existing.updatedAt === loadedUpdatedAt.value
      && JSON.stringify(existing.definition) === JSON.stringify(captured.definition)) return existing
    try {
      const entry = await workflow.saveWorkflow({
        ...captured, id: editingId.value || undefined,
        expectedUpdatedAt: options.force ? undefined : loadedUpdatedAt.value,
        force: options.force,
      })
      if (disposed || generation !== saveGeneration) return null
      workbench.adoptWorkflowKey(entry.id)
      editingId.value = entry.id
      loadedUpdatedAt.value = entry.updatedAt
      initialSnapshot.value = savedSnapshot
      saveError.value = ''
      pendingDefinition.value = null
      showRevisionConflict.value = false
      if (!options.silent) message.success(t('workflows.saved'))
      return entry
    } catch (error) {
      saveError.value = error instanceof WorkflowEditorInUseError ? t('workflows.editorInUseHint')
        : error instanceof Error ? error.message : t('workflows.saveFailed')
      if (error instanceof WorkflowRevisionConflictError) {
        pendingDefinition.value = captured.definition
        showRevisionConflict.value = true
      } else if (!options.silent) message.error(saveError.value)
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

async function save(currentDefinition?: Record<string, unknown>, options: { force?: boolean; silent?: boolean } = {}) {
  cancelAutosave()
  if (disposed) return null
  try {
    if (currentDefinition && typeof currentDefinition === 'object') definition.value = currentDefinition
    else syncCurrentDefinition()
    if (!canSave.value) return null
    const definitionToSave = storeGraphDefaults(definition.value, {
      device: defaultDevice.value,
      outputFormat: defaultFormat.value,
    })
    definition.value = definitionToSave
    return persistDefinition(definitionToSave, options)
  } catch (error) {
    saveError.value = error instanceof Error ? error.message : String(error)
    return null
  }
}

async function flushChanges() {
  cancelAutosave()
  if (runPromise) await runPromise
  await saveQueue
  try {
    syncCurrentDefinition()
    while (dirty.value || !editingId.value) {
      if (showRevisionConflict.value || !canSave.value || !await save(undefined, { silent: true })) return false
      syncCurrentDefinition()
    }
    return true
  } catch (error) {
    saveError.value = error instanceof Error ? error.message : String(error)
    return false
  }
}

async function reloadRevisionConflict() {
  if (!editingId.value) return
  await workflow.reload()
  const latest = workflows.value.find(item => item.id === editingId.value)
  if (latest) {
    loadWorkflow(latest)
    // Explicitly recreate the graph only when accepting a remote revision.
    // Saving the current editor must keep its live LiteGraph instance; changing
    // a key after every save briefly destroys the canvas and can leave a blank
    // editor window.
    editorKey.value += 1
  }
  pendingDefinition.value = null
  showRevisionConflict.value = false
}

function saveRevisionConflictCopy() {
  if (!pendingDefinition.value) return
  saveGeneration += 1
  cancelAutosave()
  saveError.value = ''
  pendingDefinition.value = null
  showRevisionConflict.value = false
  name.value = t('workflows.copyName', { name: name.value.trim() })
  workbench.adoptWorkflowKey('new-graph')
  editingId.value = ''
  loadedUpdatedAt.value = undefined
  void save()
}

function overwriteRevisionConflict() {
  if (!pendingDefinition.value) return
  saveGeneration += 1
  pendingDefinition.value = null
  showRevisionConflict.value = false
  saveError.value = ''
  void save(undefined, { force: true })
}

async function closeEditor() {
  if (closing.value) return
  finishNameEdit()
  closing.value = true
  try {
    if (loaded.value && !await flushChanges()) { if (!showRevisionConflict.value) showClosePrompt.value = true; return }
    await destroyWindow()
  } finally {
    closing.value = false
  }
}

async function destroyWindow() {
  cancelAutosave()
  if (currentWindow && currentWindow.label !== 'main') {
    try {
      await invoke('close_current_window')
    } catch {
      await currentWindow.destroy().catch(() => currentWindow.close())
    }
    return
  }
  confirmedClose = true
  await router.push('/workflows')
}

async function saveAndClose() {
  if (saving.value) return
  finishNameEdit()
  syncCurrentDefinition()
  if (!canSave.value) {
    message.warning(formError.value)
    return
  }
  showClosePrompt.value = false
  await closeEditor()
}

function beginNameEdit() {
  nameBeforeEdit.value = name.value
  editingName.value = true
  void nextTick(() => nameInputRef.value?.focus())
}

function finishNameEdit() {
  if (!editingName.value) return
  const trimmed = name.value.trim()
  name.value = trimmed || nameBeforeEdit.value.trim() || t('workflows.newWorkflow')
  editingName.value = false
}

function cancelNameEdit() {
  name.value = nameBeforeEdit.value || t('workflows.newWorkflow')
  editingName.value = false
}
async function refreshMaximized() {
  if (!currentWindow) {
    isMaximized.value = false
    return
  }
  try {
    isMaximized.value = await invoke<boolean>('is_current_window_maximized')
  } catch (error) {
    studioLog.warn('[workflow-node-editor] is_current_window_maximized failed', error)
    try {
      isMaximized.value = await currentWindow.isMaximized()
    } catch {
      isMaximized.value = false
    }
  }
}

function startWindowDrag(event?: MouseEvent) {
  if (event?.detail && event.detail > 1) {
    void toggleMaximizeWindow()
    return
  }
  if (!currentWindow) return
  invoke('start_drag_current_window').catch((error) => {
    studioLog.warn('[workflow-node-editor] start_drag_current_window failed', error)
    currentWindow.startDragging().catch(innerError => studioLog.warn('[workflow-node-editor] currentWindow.startDragging failed', innerError))
  })
}

async function minimizeWindow() {
  if (!currentWindow) return
  try {
    await invoke('minimize_current_window')
  } catch (error) {
    studioLog.warn('[workflow-node-editor] minimize_current_window failed', error)
    currentWindow.minimize().catch(innerError => studioLog.warn('[workflow-node-editor] currentWindow.minimize failed', innerError))
  }
}

async function toggleMaximizeWindow() {
  if (!currentWindow) return
  try {
    isMaximized.value = await invoke<boolean>('toggle_maximize_current_window')
  } catch (error) {
    studioLog.warn('[workflow-node-editor] toggle_maximize_current_window failed', error)
    currentWindow.toggleMaximize().then(refreshMaximized).catch(innerError => studioLog.warn('[workflow-node-editor] currentWindow.toggleMaximize failed', innerError))
  }
}

let unlistenResize: (() => void) | undefined
let unlistenCloseRequested: (() => void) | undefined

onMounted(async () => {
  // Standalone node-editor windows are opened directly at this route, which can
  // mount before bootstrap's `workflows.initialize()` finishes. Waiting here keeps
  // the editor from falling back to a blank draft when a workflowId is in the URL.
  await Promise.allSettled([workflow.initialize(), model.initialize()])
  if (disposed) return
  const workflowId = String(route.query.workflowId || '')
  const isNewWorkflow = route.query.new === '1'
  const target = workflowId ? workflows.value.find(item => item.id === workflowId) : null
  if (!isNewWorkflow && workflowId && !target) {
    loadWorkflow(null)
    editingId.value = workflowId
    formatError.value = t('workflows.workflowNotFound')
  } else {
    loadWorkflow(isNewWorkflow ? null : target || workflows.value.find(item => item.id === workflow.selectedWorkflowId) || workflows.value[0] || null)
  }
  captureInitialSnapshot()
  loaded.value = true
  await refreshMaximized()
  if (!currentWindow || currentWindow.label === 'main') return
  try {
    unlistenResize = await currentWindow.onResized(refreshMaximized)
  } catch {}
  try {
    unlistenCloseRequested = await currentWindow.onCloseRequested((event) => {
      event.preventDefault()
      void closeEditor()
    })
  } catch {}
})

watch(snapshot, () => {
  if (!showRevisionConflict.value) saveError.value = ''
  scheduleAutosave()
}, { flush: 'post' })
watch([loaded, canvasInitialized, saving, submitting, editingName, closing, showClosePrompt, showRevisionConflict], scheduleAutosave, { flush: 'post' })

onBeforeUnmount(() => {
  disposed = true
  saveGeneration += 1
  cancelAutosave()
  unlistenResize?.()
  unlistenCloseRequested?.()
})

</script>

<template>
  <div
    class="workflow-node-editor-page"
    :class="{ 'workflow-node-editor-page--custom-chrome': showCustomWindowChrome }"
  >
    <header v-if="showCustomWindowChrome" class="workflow-window-chrome">
      <div class="workflow-window-chrome__drag" data-tauri-drag-region @mousedown.left="startWindowDrag">
        <div class="workflow-window-chrome__copy">
          <strong>{{ t('app.name') }}</strong>
          <span class="workflow-window-chrome__divider">·</span>
          <span>{{ t('workflows.nodeEditor') }}</span>
        </div>
      </div>
      <div class="workflow-window-chrome__actions">
        <button type="button" :aria-label="t('common.minimize')" @click="minimizeWindow">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M2 6h8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        </button>
        <button type="button" :aria-label="t('common.maximize')" @click="toggleMaximizeWindow">
          <svg v-if="isMaximized" width="12" height="12" viewBox="0 0 12 12" fill="none"><rect x="1.5" y="3.5" width="7" height="7" rx="1" stroke="currentColor" stroke-width="1.2"/><path d="M3.5 3.5V2a.5.5 0 01.5-.5h6a.5.5 0 01.5.5v6a.5.5 0 01-.5.5h-1.5" stroke="currentColor" stroke-width="1.2"/></svg>
          <svg v-else width="12" height="12" viewBox="0 0 12 12" fill="none"><rect x="2" y="2" width="8" height="8" rx="1.5" stroke="currentColor" stroke-width="1.2"/></svg>
        </button>
        <button type="button" class="danger" :aria-label="t('common.close')" @click="closeEditor">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        </button>
      </div>
    </header>

    <div v-if="loaded && formatError" class="workflow-format-error">
      <n-alert type="error" :title="t('workflows.graphEditorFormatErrorTitle')">
        {{ formatError }}
      </n-alert>
      <n-button type="primary" @click="closeEditor">{{ t('common.close') }}</n-button>
    </div>

    <WorkflowNodeEditor v-else-if="loaded" ref="editorRef" :key="editorKey" v-model:definition="definition"
      :create-starter="!editingId"
      :running-node-ids="runningNodeIds"
      :model-options="modelOptions" :models="models" :default-device="defaultDevice" :default-format="defaultFormat"
      :form-error="formError" :can-save="canSave && !saving && !submitting && !closing" :saving="saving"
      :advisory="missingModelNodes.length ? t('workflows.stepModelNotDownloaded', { id: missingModelNodes.join(', ') }) : ''"
      @save="save" @run="runWorkflow"
      @close="closeEditor" @initialized="initializeEditorBaseline" @defaults-restored="restoreDefaultControls" @load-error="canvasError = $event">
      <template #title>
        <div class="workflow-node-editor-title">
          <button v-if="!editingName" type="button" class="workflow-name-trigger" :title="name" @click="beginNameEdit">
            {{ name || t('workflows.untitled') }}
          </button>
          <n-input
            v-else
            ref="nameInputRef"
            v-model:value="name"
            class="workflow-name-input"
            size="small"
            :placeholder="t('workflows.namePlaceholder')"
            @blur="finishNameEdit"
            @keydown.enter.prevent="finishNameEdit"
            @keydown.esc.prevent="cancelNameEdit"
          />
          <span v-if="saving" class="workflow-save-status">{{ t('workflows.autoSaveSaving') }}</span>
          <n-button v-else-if="saveError" size="tiny" quaternary type="error" :title="saveError" @click="save()">{{ t('workflows.autoSaveFailed') }} · {{ t('common.retry') }}</n-button>
          <span v-else class="workflow-save-status">{{ dirty || !editingId ? t('workflows.autoSavePending') : t('workflows.autoSaveSaved') }}</span>
        </div>
      </template>
      <template #settings-controls>
        <n-button size="small" quaternary @click="showOutputSettings = true"><template #icon><n-icon :component="FolderOpenOutline" /></template>{{ t('separate.output') }}</n-button>
      </template>
      <template #execution-progress><WorkflowExecutionProgress :job="workbench.focusedJob.value" :task="workbench.focusedTask.value" /></template>
      <template #run-controls>
        <WorkflowRunPanel :workflow-key="workflowKey" v-model:job-id="selectedJobId" size="small"
          :submitting="submitting" :can-run="canRun" :run-primary="true" :show-status="false"
          @run="runWorkflow" @cancel="workbench.cancel">

        </WorkflowRunPanel>
      </template>
    </WorkflowNodeEditor>
    <n-modal v-model:show="showOutputSettings" preset="card" :title="t('separate.output')" class="advanced-output-settings-modal" style="width: min(480px, calc(100vw - 32px))">
      <WorkflowNodeBindings :workflow-key="workflowKey" :definition="definition" mode="outputs" :aggregate-output="true" />
      <template #footer><div class="workflow-node-editor-page__prompt-actions"><n-button size="small" secondary @click="showOutputSettings = false">{{ t('common.close') }}</n-button></div></template>
    </n-modal>
    <n-modal v-model:show="showClosePrompt" preset="card" :title="t('workflows.simpleUnsavedTitle')" style="width: min(440px, calc(100vw - 32px))">
      <p>{{ t('workflows.simpleUnsavedHint') }}</p>
      <template #footer>
        <div class="workflow-node-editor-page__prompt-actions">
          <n-button secondary @click="showClosePrompt = false; void destroyWindow()">{{ t('workflows.simpleDiscardChanges') }}</n-button>
          <n-button type="primary" :loading="saving" :disabled="saving" @click="void saveAndClose()">{{ t('workflows.simpleSaveAndClose') }}</n-button>
        </div>
      </template>
    </n-modal>

    <WorkflowRevisionConflictModal
      v-model:show="showRevisionConflict"
      :workflow-name="name"
      @reload="reloadRevisionConflict"
      @save-copy="saveRevisionConflictCopy"
      @overwrite="overwriteRevisionConflict"
    />
  </div>
</template>

<style scoped>
.workflow-node-editor-page {
  height: 100%;
  min-height: 0;
  padding: 0 12px 12px;
  display: grid;
  grid-template-rows: minmax(0, 1fr);
  gap: 10px;
  background: var(--surface);
  color: var(--on-surface);
  -webkit-app-region: no-drag;
}

.workflow-format-error {
  align-self: start;
  display: grid;
  justify-items: start;
  gap: 14px;
  width: min(640px, 100%);
  margin: 48px auto 0;
}

.workflow-node-editor-page__prompt-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.workflow-node-editor-page--custom-chrome {
  grid-template-rows: auto minmax(0, 1fr);
}

.workflow-node-editor-page *,
.workflow-node-editor-page :deep(*) {
  -webkit-app-region: no-drag;
}

.workflow-window-chrome {
  height: 40px;
  margin-inline: -12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid color-mix(in srgb, var(--outline) 54%, transparent);
  background: color-mix(in srgb, var(--surface) 84%, transparent);
  backdrop-filter: blur(18px) saturate(1.15);
  user-select: none;
}

.workflow-window-chrome__drag {
  flex: 1;
  height: 100%;
  display: flex;
  align-items: center;
  padding-left: 12px;
  -webkit-app-region: drag;
}

.workflow-window-chrome__copy {
  display: flex;
  align-items: center;
  gap: 6px;
}

.workflow-window-chrome__copy strong {
  font-size: 12px;
  line-height: 1.1;
}

.workflow-window-chrome__copy span {
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.1;
}

.workflow-window-chrome__divider {
  font-size: 11px;
  opacity: 0.7;
}

.workflow-window-chrome__actions {
  display: flex;
  height: 100%;
}

.workflow-window-chrome__actions button {
  width: 46px;
  border: 0;
  background: transparent;
  color: var(--on-surface-muted);
  display: grid;
  place-items: center;
  cursor: pointer;
}

.workflow-window-chrome__actions button:hover {
  background: color-mix(in srgb, var(--surface-2) 88%, transparent);
  color: var(--on-surface);
}

.workflow-window-chrome__actions button.danger:hover {
  background: var(--danger);
  color: #fff;
}

.workflow-node-editor-title { min-width: 0; display: flex; align-items: center; gap: 10px; }
.workflow-save-status { flex-shrink: 0; color: var(--on-surface-muted); font-size: 11px; white-space: nowrap; }

.workflow-name-trigger {
  min-width: 0;
  width: fit-content;
  max-width: min(360px, 100%);
  padding: 0;
  border: 0;
  color: var(--on-surface);
  background: transparent;
  cursor: text;
  font: inherit;
  font-size: 18px;
  font-weight: 800;
  line-height: 1.12;
  text-align: left;
}

.workflow-name-trigger:hover {
  color: var(--primary-strong);
}

.workflow-name-trigger,
.workflow-name-input {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.workflow-name-input {
  max-width: 320px;
}

</style>
