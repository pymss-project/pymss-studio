import { createLogger } from '@/utils/logger'
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { hasBackendBridge as isTauriRuntime, loadAppStore, saveAppStore } from '@/utils/appStore'
import { isWorkflowEditorSnapshot, workflowEditorFor, WorkflowEditorInUseError, type WorkflowEditorSnapshot, type WorkflowEditorRecord } from '@/utils/workflowEditorState'
import {
  detectWorkflowFormat,
  normalizeGraphWorkflowDefinition,
  normalizeSimpleWorkflowDefinition,
  WORKFLOW_FORMAT_VERSION,
  type WorkflowFormat,
} from '@/workflows/formats'

const studioLog = createLogger('ui.stores.workflow')

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

type WorkflowStoreMutation = {
  action: 'upsert' | 'delete' | 'select'
  entry?: WorkflowEntry
  workflowId?: string
  expectedUpdatedAt?: number
  legacyIndex?: number
  legacyEntry?: Partial<WorkflowEntry>
  force?: boolean
}

type WorkflowMutationResponse = {
  state: StoredWorkflowState
  editorSnapshot?: WorkflowEditorSnapshot
  conflict?: {
    workflowId: string
    expectedUpdatedAt: number
    actualUpdatedAt: number
  }
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
    updatedAt: Number.isFinite(Number(input.updatedAt)) ? Number(input.updatedAt) : 0,
  }
}

export const useWorkflowStore = defineStore('workflow', () => {
  const workflows = ref<WorkflowEntry[]>([])
  const selectedWorkflowId = ref('')
  const nodeEditorOpenWorkflowId = ref('')
  const simpleEditorOpenWorkflowId = ref('')
  const editorSnapshot = ref<WorkflowEditorSnapshot | null>(null)
  const openedEditors = computed<WorkflowEditorRecord[]>(() => editorSnapshot.value?.editors || [
    ...(nodeEditorOpenWorkflowId.value ? [{ label: `workflow-node-editor-${nodeEditorOpenWorkflowId.value}`, workflowId: nodeEditorOpenWorkflowId.value === '__new__' ? '' : nodeEditorOpenWorkflowId.value, kind: 'advanced' as const }] : []),
    ...(simpleEditorOpenWorkflowId.value ? [{ label: `workflow-simple-editor-${simpleEditorOpenWorkflowId.value}`, workflowId: simpleEditorOpenWorkflowId.value === '__new__' ? '' : simpleEditorOpenWorkflowId.value, kind: 'simple' as const }] : []),
  ])
  const hasAnyEditorOpen = computed(() => openedEditors.value.length > 0)

  function applyEditorSnapshot(value: unknown) {
    if (!isWorkflowEditorSnapshot(value) || (editorSnapshot.value && value.revision <= editorSnapshot.value.revision)) return false
    editorSnapshot.value = { revision: value.revision, editors: value.editors.map(editor => ({ ...editor })) }
    nodeEditorOpenWorkflowId.value = value.editors.filter(editor => editor.kind === 'advanced').at(-1)?.workflowId || (value.editors.some(editor => editor.kind === 'advanced') ? '__new__' : '')
    simpleEditorOpenWorkflowId.value = value.editors.filter(editor => editor.kind === 'simple').at(-1)?.workflowId || (value.editors.some(editor => editor.kind === 'simple') ? '__new__' : '')
    return true
  }

  async function refreshEditorWindows() {
    if (isTauriRuntime()) applyEditorSnapshot(await invoke('get_workflow_editors'))
  }

  function recordEditorOpened(result: unknown, kind: 'simple' | 'advanced', workflowId: string) {
    const snapshot = (result as { editorSnapshot?: unknown } | null)?.editorSnapshot
    if (isWorkflowEditorSnapshot(snapshot)) { applyEditorSnapshot(snapshot); return }
    if (kind === 'simple') markSimpleEditorOpen(workflowId)
    else markNodeEditorOpen(workflowId)
  }

  function editorFor(workflowId: string) { return workflowEditorFor(openedEditors.value, workflowId) }
  const initialized = ref(false)
  const isSaving = ref(false)
  const selectedWorkflow = computed(() => workflows.value.find(item => item.id === selectedWorkflowId.value) || null)
  let pendingPersistCount = 0
  let mutationQueue = Promise.resolve()
  let selectionMutationGeneration = 0
  let legacyWorkflowReferences = new Map<string, { index: number; entry: Partial<WorkflowEntry> }>()
  // Bootstrap and standalone node-editor windows can call initialize() at the
  // same time. Share one in-flight load so a slower second read cannot race
  // the first one and leave the editor with an empty/stale workflow list.
  let initializationPromise: Promise<void> | null = null
  let editorCloseRefreshPromise: Promise<WorkflowEntry | null> | null = null
  let editorCloseRefreshGeneration = 0

  function queueMutation<T>(operation: () => Promise<T>) {
    pendingPersistCount += 1
    isSaving.value = true
    const run = mutationQueue.catch(() => undefined).then(operation)
    mutationQueue = run.then(() => undefined, () => undefined)
    return run.finally(() => {
      pendingPersistCount -= 1
      isSaving.value = pendingPersistCount > 0
    })
  }

  function normalizeStoredState(stored: StoredWorkflowState | null | undefined): LoadedWorkflowState {
    const nextLegacyReferences = new Map<string, { index: number; entry: Partial<WorkflowEntry> }>()
    const loadedWorkflows = (stored?.workflows || [])
      .map((item, index) => {
        const normalized = normalizeWorkflow(item)
        if (normalized && !String(item.id || '').trim()) {
          nextLegacyReferences.set(normalized.id, {
            index,
            entry: JSON.parse(JSON.stringify(item)) as Partial<WorkflowEntry>,
          })
        }
        return normalized
      })
      .filter((item): item is WorkflowEntry => Boolean(item))
      .sort((a, b) => b.updatedAt - a.updatedAt)
    const storedSelectedId = String(stored?.selectedWorkflowId || '')
    const loadedSelectedId = loadedWorkflows.some(item => item.id === storedSelectedId)
      ? storedSelectedId
      : loadedWorkflows[0]?.id || ''
    legacyWorkflowReferences = nextLegacyReferences
    return { workflows: loadedWorkflows, selectedWorkflowId: loadedSelectedId }
  }

  async function readStoredState(throwOnError = false): Promise<LoadedWorkflowState> {
    const stored = await loadAppStore<StoredWorkflowState>('workflow-state').catch((error) => {
      if (throwOnError) throw error
      return null
    })
    return normalizeStoredState(stored)
  }

  function findMutationWorkflowIndex(workflowsToSearch: WorkflowEntry[], mutation: WorkflowStoreMutation) {
    const workflowId = mutation.entry?.id || mutation.workflowId || ''
    const exactIndex = workflowsToSearch.findIndex(item => item.id === workflowId)
    if (exactIndex >= 0) return exactIndex
    if (mutation.legacyIndex === undefined && !mutation.legacyEntry) return -1
    const legacyId = [...legacyWorkflowReferences.entries()].find(([, reference]) => (
      (mutation.legacyIndex === undefined || reference.index === mutation.legacyIndex)
      && (!mutation.legacyEntry
        || JSON.stringify(reference.entry) === JSON.stringify(mutation.legacyEntry))
    ))?.[0]
    return legacyId ? workflowsToSearch.findIndex(item => item.id === legacyId) : -1
  }

  async function mutateStoredState(mutation: WorkflowStoreMutation): Promise<WorkflowMutationResponse> {
    if (isTauriRuntime()) {
      try {
        const response = await invoke<WorkflowMutationResponse>('mutate_workflow_store', { payload: mutation })
        if (response.editorSnapshot) applyEditorSnapshot(response.editorSnapshot)
        return response
      } catch (error) {
        if (String(error).includes('WORKFLOW_EDITOR_IN_USE')) throw new WorkflowEditorInUseError()
        throw error
      }
    }

    const current = await readStoredState(true)
    const next: LoadedWorkflowState = {
      workflows: current.workflows.map(item => JSON.parse(JSON.stringify(item)) as WorkflowEntry),
      selectedWorkflowId: current.selectedWorkflowId,
    }
    if (mutation.action === 'upsert' && mutation.entry) {
      const index = findMutationWorkflowIndex(next.workflows, mutation)
      const legacyTargetMismatch = index < 0
        && (mutation.legacyIndex !== undefined || Boolean(mutation.legacyEntry))
      const existing = index >= 0 ? next.workflows[index] : null
      const actualUpdatedAt = existing?.updatedAt || 0
      if (
        !mutation.force
        && (legacyTargetMismatch
          || (mutation.expectedUpdatedAt !== undefined
            && mutation.expectedUpdatedAt !== actualUpdatedAt))
      ) {
        return {
          state: next,
          conflict: {
            workflowId: mutation.entry.id,
            expectedUpdatedAt: mutation.expectedUpdatedAt ?? 0,
            actualUpdatedAt,
          },
        }
      }
      const entry = {
        ...mutation.entry,
        createdAt: existing?.createdAt || mutation.entry.createdAt,
        updatedAt: Math.max(mutation.entry.updatedAt, actualUpdatedAt + 1),
      }
      if (index >= 0) next.workflows.splice(index, 1, entry)
      else next.workflows.push(entry)
      next.workflows.sort((a, b) => b.updatedAt - a.updatedAt)
      next.selectedWorkflowId = entry.id
    } else if (mutation.action === 'delete') {
      const index = findMutationWorkflowIndex(next.workflows, mutation)
      const actualUpdatedAt = index >= 0 ? next.workflows[index].updatedAt : 0
      if (
        (index < 0 && (mutation.legacyIndex !== undefined || mutation.legacyEntry))
        || (mutation.expectedUpdatedAt !== undefined
          && mutation.expectedUpdatedAt !== actualUpdatedAt)
      ) {
        return {
          state: next,
          conflict: {
            workflowId: mutation.workflowId || '',
            expectedUpdatedAt: mutation.expectedUpdatedAt ?? 0,
            actualUpdatedAt,
          },
        }
      }
      if (index >= 0) {
        const [removed] = next.workflows.splice(index, 1)
        if (next.selectedWorkflowId === removed.id || next.selectedWorkflowId === mutation.workflowId) {
          next.selectedWorkflowId = next.workflows[0]?.id || ''
        }
      }
    } else if (mutation.action === 'select') {
      if (!mutation.workflowId) {
        next.selectedWorkflowId = ''
      } else {
        const index = findMutationWorkflowIndex(next.workflows, mutation)
        if (index < 0 && (mutation.legacyIndex !== undefined || mutation.legacyEntry)) {
          return {
            state: next,
            conflict: {
              workflowId: mutation.workflowId,
              expectedUpdatedAt: 0,
              actualUpdatedAt: 0,
            },
          }
        }
        if (index >= 0) {
          next.workflows[index].id = mutation.workflowId
          next.selectedWorkflowId = mutation.workflowId
        }
      }
    }
    const stored: StoredWorkflowState = JSON.parse(JSON.stringify(next)) as StoredWorkflowState
    await saveAppStore('workflow-state', stored)
    return { state: stored }
  }

  function applyStoredState(state: LoadedWorkflowState) {
    workflows.value = state.workflows
    selectedWorkflowId.value = state.selectedWorkflowId
    initialized.value = true
  }

  function normalizeMutationState(
    stored: StoredWorkflowState,
    selectionGenerationAtRequest: number,
  ) {
    const latest = normalizeStoredState(stored)
    if (selectionGenerationAtRequest !== selectionMutationGeneration) {
      const currentId = selectedWorkflowId.value
      if (!currentId || latest.workflows.some(item => item.id === currentId)) {
        latest.selectedWorkflowId = currentId
      }
    }
    return latest
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

  function handleEditorClosed(kind: 'advanced' | 'simple', _label?: string) {
    const registryRefresh = editorSnapshot.value ? refreshEditorWindows() : Promise.resolve()
    if (!editorSnapshot.value) {
      if (kind === 'advanced') markNodeEditorClosed()
      else markSimpleEditorClosed()
    }
    editorCloseRefreshGeneration += 1
    if (!editorCloseRefreshPromise) {
      editorCloseRefreshPromise = refreshAfterEditorClosed()
    }
    return editorSnapshot.value ? Promise.all([registryRefresh, editorCloseRefreshPromise]).then(([, selected]) => selected) : editorCloseRefreshPromise
  }

  async function saveWorkflow(input: SaveWorkflowInput) {
    const selectionGenerationAtRequest = selectionMutationGeneration
    const legacyReference = input.id ? legacyWorkflowReferences.get(input.id) : undefined
    return queueMutation(async () => {
      const existing = input.id ? workflows.value.find(item => item.id === input.id) : null
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
      const response = await mutateStoredState({
        action: 'upsert',
        entry,
        expectedUpdatedAt: input.expectedUpdatedAt,
        legacyIndex: legacyReference?.index,
        legacyEntry: legacyReference?.entry,
        force: input.force,
      })
      const latest = normalizeMutationState(response.state, selectionGenerationAtRequest)
      applyStoredState(latest)
      if (response.conflict) {
        throw new WorkflowRevisionConflictError(
          response.conflict.workflowId,
          response.conflict.expectedUpdatedAt,
          response.conflict.actualUpdatedAt,
        )
      }
      const saved = workflows.value.find(item => item.id === entry.id)
      if (!saved) throw new Error('Saved workflow is missing from persistent state')
      return saved
    })
  }

  async function deleteWorkflow(id: string) {
    const selectionGenerationAtRequest = selectionMutationGeneration
    const expectedUpdatedAt = workflows.value.find(item => item.id === id)?.updatedAt
    const legacyReference = legacyWorkflowReferences.get(id)
    await queueMutation(async () => {
      const response = await mutateStoredState({
        action: 'delete',
        workflowId: id,
        expectedUpdatedAt,
        legacyIndex: legacyReference?.index,
        legacyEntry: legacyReference?.entry,
      })
      applyStoredState(normalizeMutationState(response.state, selectionGenerationAtRequest))
      if (response.conflict) {
        throw new WorkflowRevisionConflictError(
          response.conflict.workflowId,
          response.conflict.expectedUpdatedAt,
          response.conflict.actualUpdatedAt,
        )
      }
      if (nodeEditorOpenWorkflowId.value === id) nodeEditorOpenWorkflowId.value = ''
      if (simpleEditorOpenWorkflowId.value === id) simpleEditorOpenWorkflowId.value = ''
    })
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
    const nextId = workflows.value.some(item => item.id === id) ? id : ''
    const generation = ++selectionMutationGeneration
    const legacyReference = legacyWorkflowReferences.get(nextId)
    selectedWorkflowId.value = nextId
    void queueMutation(async () => {
      const response = await mutateStoredState({
        action: 'select',
        workflowId: nextId,
        legacyIndex: legacyReference?.index,
        legacyEntry: legacyReference?.entry,
      })
      const latest = normalizeMutationState(response.state, generation)
      if (generation === selectionMutationGeneration && !nextId) latest.selectedWorkflowId = ''
      applyStoredState(latest)
      if (response.conflict) {
        throw new WorkflowRevisionConflictError(
          response.conflict.workflowId,
          response.conflict.expectedUpdatedAt,
          response.conflict.actualUpdatedAt,
        )
      }
    }).catch((error) => {
      studioLog.warn('Failed to persist workflow selection:', error)
    })
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
    openedEditors,
    hasAnyEditorOpen,
    applyEditorSnapshot,
    refreshEditorWindows,
    recordEditorOpened,
    editorFor,
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
