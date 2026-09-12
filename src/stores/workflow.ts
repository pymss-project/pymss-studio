import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { loadAppStore, saveAppStore } from '@/utils/appStore'
import {
  detectWorkflowFormat,
  normalizeGraphWorkflowDefinition,
  normalizeSimpleWorkflowDefinition,
  WORKFLOW_FORMAT_VERSION,
  type WorkflowFormat,
} from '@/workflows/formats'

export type WorkflowEntry = {
  id: string
  name: string
  description: string
  definition: Record<string, unknown>
  format: WorkflowFormat
  formatVersion: number
  createdAt: number
  updatedAt: number
}

export class WorkflowRevisionConflictError extends Error {
  readonly code = 'WORKFLOW_REVISION_CONFLICT'

  constructor(
    readonly workflowId: string,
    readonly expectedUpdatedAt: number,
    readonly actualUpdatedAt: number,
  ) {
    super('Workflow was modified by another editor')
    this.name = 'WorkflowRevisionConflictError'
  }
}

export type SaveWorkflowInput = {
  id?: string
  name: string
  description?: string
  definition: Record<string, unknown>
  format?: WorkflowFormat
  formatVersion?: number
  expectedUpdatedAt?: number
  force?: boolean
}

type StoredWorkflowState = {
  workflows?: Partial<WorkflowEntry>[]
  selectedWorkflowId?: string
}

type LoadedWorkflowState = {
  workflows: WorkflowEntry[]
  selectedWorkflowId: string
}

function createId(prefix = 'workflow') {
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`
}

function normalizeDefinition(value: unknown): Record<string, unknown> {
  // Workflows are now stored as native comfy-mss JSON (node editor) or pymss
  // YAML dict (simple creator). Keep format compatibility migrations at this
  // storage boundary so every consumer sees the current schema.
  const definition = value && typeof value === 'object' && !Array.isArray(value)
    ? JSON.parse(JSON.stringify(value)) as Record<string, unknown>
    : {}
  return detectWorkflowFormat(definition) === 'simple'
    ? normalizeSimpleWorkflowDefinition(definition)
    : normalizeGraphWorkflowDefinition(definition)
}

function normalizeWorkflow(input: Partial<WorkflowEntry>): WorkflowEntry | null {
  const name = String(input.name || '').trim()
  const id = String(input.id || '').trim() || createId()
  if (!name) return null
  const now = Date.now()
  const definition = normalizeDefinition(input.definition)
  const detectedFormat = detectWorkflowFormat(definition)
  return {
    id,
    name,
    description: String(input.description || '').trim(),
    definition,
    format: detectedFormat === 'unknown' ? (input.format || 'unknown') : detectedFormat,
    formatVersion: Number.isFinite(Number(input.formatVersion))
      ? Number(input.formatVersion)
      : WORKFLOW_FORMAT_VERSION,
    createdAt: Number.isFinite(Number(input.createdAt)) ? Number(input.createdAt) : now,
    updatedAt: Number.isFinite(Number(input.updatedAt)) ? Number(input.updatedAt) : now,
  }
}

export const useWorkflowStore = defineStore('workflow', () => {
  const workflows = ref<WorkflowEntry[]>([])
  const selectedWorkflowId = ref('')
  const nodeEditorOpenWorkflowId = ref('')
  const simpleEditorOpenWorkflowId = ref('')
  const initialized = ref(false)
  const isSaving = ref(false)
  const selectedWorkflow = computed(() => workflows.value.find(item => item.id === selectedWorkflowId.value) || null)
  let persistQueue = Promise.resolve()
  let pendingPersistCount = 0
  // Bootstrap and standalone node-editor windows can call initialize() at the
  // same time. Share one in-flight load so a slower second read cannot race
  // the first one and leave the editor with an empty/stale workflow list.
  let initializationPromise: Promise<void> | null = null
  let editorCloseRefreshPromise: Promise<WorkflowEntry | null> | null = null
  let editorCloseRefreshGeneration = 0

  function persist() {
    const snapshot = JSON.parse(JSON.stringify({
      workflows: workflows.value,
      selectedWorkflowId: selectedWorkflowId.value,
    })) as StoredWorkflowState
    pendingPersistCount += 1
    isSaving.value = true
    const run = persistQueue.then(() => saveAppStore('workflow-state', snapshot))
    persistQueue = run.catch(() => undefined)
    return run.finally(() => {
      pendingPersistCount -= 1
      isSaving.value = pendingPersistCount > 0
    })
  }

  async function readStoredState(throwOnError = false): Promise<LoadedWorkflowState> {
    const stored = await loadAppStore<StoredWorkflowState>('workflow-state').catch((error) => {
      if (throwOnError) throw error
      return null
    })
    const loadedWorkflows = (stored?.workflows || [])
      .map(item => normalizeWorkflow(item))
      .filter((item): item is WorkflowEntry => Boolean(item))
      .sort((a, b) => b.updatedAt - a.updatedAt)
    const storedSelectedId = String(stored?.selectedWorkflowId || '')
    const loadedSelectedId = loadedWorkflows.some(item => item.id === storedSelectedId)
      ? storedSelectedId
      : loadedWorkflows[0]?.id || ''
    return { workflows: loadedWorkflows, selectedWorkflowId: loadedSelectedId }
  }

  function applyStoredState(state: LoadedWorkflowState) {
    workflows.value = state.workflows
    selectedWorkflowId.value = state.selectedWorkflowId
    initialized.value = true
  }

  async function loadStoredState(throwOnError = false) {
    applyStoredState(await readStoredState(throwOnError))
  }

  async function initialize() {
    if (initialized.value) return
    if (!initializationPromise) {
      initializationPromise = loadStoredState().finally(() => {
        initializationPromise = null
      })
    }
    await initializationPromise
  }

  async function reload(options: { throwOnError?: boolean } = {}) {
    await loadStoredState(options.throwOnError)
  }

  async function refreshAfterEditorClosed() {
    try {
      while (true) {
        const generation = editorCloseRefreshGeneration
        const state = await readStoredState(true)
        // Another editor may have saved after this read captured its snapshot.
        // Read again without publishing obsolete state to the overview.
        if (generation !== editorCloseRefreshGeneration) continue
        applyStoredState(state)
        return selectedWorkflow.value
      }
    } finally {
      editorCloseRefreshPromise = null
    }
  }

  function handleEditorClosed(kind: 'advanced' | 'simple') {
    if (kind === 'advanced') markNodeEditorClosed()
    else markSimpleEditorClosed()
    editorCloseRefreshGeneration += 1
    if (!editorCloseRefreshPromise) {
      editorCloseRefreshPromise = refreshAfterEditorClosed()
    }
    return editorCloseRefreshPromise
  }

  async function saveWorkflow(input: SaveWorkflowInput) {
    const existing = input.id ? workflows.value.find(item => item.id === input.id) : null
    if (input.id && input.expectedUpdatedAt !== undefined && !existing && !input.force) {
      throw new WorkflowRevisionConflictError(input.id, input.expectedUpdatedAt, 0)
    }
    if (
      existing
      && input.expectedUpdatedAt !== undefined
      && input.expectedUpdatedAt !== existing.updatedAt
      && !input.force
    ) {
      throw new WorkflowRevisionConflictError(existing.id, input.expectedUpdatedAt, existing.updatedAt)
    }
    const now = Math.max(Date.now(), (existing?.updatedAt || 0) + 1)
    const definition = normalizeDefinition(input.definition)
    const detectedFormat = detectWorkflowFormat(definition)
    const entry: WorkflowEntry = {
      id: existing?.id || input.id || createId(),
      name: input.name.trim(),
      description: String(input.description || '').trim(),
      definition,
      format: detectedFormat === 'unknown'
        ? (input.format || existing?.format || 'unknown')
        : detectedFormat,
      formatVersion: Number.isFinite(Number(input.formatVersion))
        ? Number(input.formatVersion)
        : existing?.formatVersion || WORKFLOW_FORMAT_VERSION,
      createdAt: existing?.createdAt || now,
      updatedAt: now,
    }
    if (!entry.name) throw new Error('Workflow name is required')
    const index = workflows.value.findIndex(item => item.id === entry.id)
    if (index >= 0) workflows.value.splice(index, 1, entry)
    else workflows.value.unshift(entry)
    workflows.value.sort((a, b) => b.updatedAt - a.updatedAt)
    selectedWorkflowId.value = entry.id
    await persist()
    return entry
  }

  async function deleteWorkflow(id: string) {
    workflows.value = workflows.value.filter(item => item.id !== id)
    if (selectedWorkflowId.value === id) selectedWorkflowId.value = workflows.value[0]?.id || ''
    if (nodeEditorOpenWorkflowId.value === id) nodeEditorOpenWorkflowId.value = ''
    if (simpleEditorOpenWorkflowId.value === id) simpleEditorOpenWorkflowId.value = ''
    await persist()
  }

  async function duplicateWorkflow(id: string) {
    const source = workflows.value.find(item => item.id === id)
    if (!source) return null
    return saveWorkflow({
      name: `${source.name} Copy`,
      description: source.description,
      definition: JSON.parse(JSON.stringify(source.definition)) as Record<string, unknown>,
      format: source.format,
      formatVersion: source.formatVersion,
    })
  }

  function selectWorkflow(id: string) {
    selectedWorkflowId.value = workflows.value.some(item => item.id === id) ? id : ''
    void persist()
  }

  function markNodeEditorOpen(workflowId: string) {
    nodeEditorOpenWorkflowId.value = workflowId
  }

  function markNodeEditorClosed() {
    nodeEditorOpenWorkflowId.value = ''
  }

  function markSimpleEditorOpen(workflowId: string) {
    simpleEditorOpenWorkflowId.value = workflowId
  }

  function markSimpleEditorClosed() {
    simpleEditorOpenWorkflowId.value = ''
  }

  return {
    workflows,
    selectedWorkflowId,
    nodeEditorOpenWorkflowId,
    simpleEditorOpenWorkflowId,
    selectedWorkflow,
    initialized,
    isSaving,
    initialize,
    reload,
    handleEditorClosed,
    saveWorkflow,
    deleteWorkflow,
    duplicateWorkflow,
    selectWorkflow,
    markNodeEditorOpen,
    markNodeEditorClosed,
    markSimpleEditorOpen,
    markSimpleEditorClosed,
  }
})
