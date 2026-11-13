import { computed, ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { registerWindowCloseGuard } from '@/utils/windowCloseGuards'
import { localLogRecord, type LogRecord } from '@/utils/logRecord'
import { getLogLineLevel } from '@/utils/taskLog'
import type { PluginCategory, PluginLocales } from './presentation'
import { useSettingsStore } from '@/stores/settings'
import { canApplyPluginUpdate, canCheckPluginUpdate, pluginUpdateKey, type PluginUpdateSelection, type PluginUpdateState } from './updates'
import { installedPluginKey } from './library'
import { isRuntimeAdmissionBusyError, retryRuntimeQuery } from '@/utils/runtimeQueries'

export type PluginRequirements = {
  sdkVersion: number | null
  requiredCapabilities: string[]
  platforms: string[]
  architectures: string[]
  backends: string[]
}
export type PluginCompatibility = { compatible: boolean; reasons: string[] }
export type CatalogPlugin = {
  name: string
  version?: string | null
  url: string
  subpath?: string | null
  description: string
  tags: string[]
  category?: PluginCategory | null
  locales?: PluginLocales
  requirements?: PluginRequirements | null
  compatibility?: PluginCompatibility
}
export type InstalledPlugin = {
  name: string
  pluginId?: string
  version: string | null
  source: string
  url: string | null
  path: string
  loaded: boolean | null
  managed: boolean
  error: string | null
  errorKind?: 'load' | 'dependency' | 'compatibility' | null
  operations: string[] | null
  operationTitles?: Record<string, string>
  description?: string
  locales?: PluginLocales
  enabled?: boolean
  prepared?: boolean
  availability?: 'ready' | 'unavailable' | 'not_prepared'
  removalPending?: boolean
  cleanupRuntime?: string | null
  sourceSelectionRequired?: boolean
  availableBindings?: { runtime: string; version: string | null; content: string | null }[]
  preinstalled?: boolean
  toolCategory?: 'convert' | 'analyze' | 'recognize' | 'edit' | null
  toolIcon?: string | null
  dependencyError?: string | null
  viewError?: string | null
  viewErrorKind?: 'load' | 'compatibility' | null
  requirements?: PluginRequirements | null
  compatibility?: PluginCompatibility
  resolvedCommit?: string | null
  contentDigest?: string | null
  installedAt?: string | null
  sourceRef?: string | null
  sourceSubpath?: string | null
  contributions?: {
    graphs?: { id: string; file: string; operation: string }[]
    views?: { id: string; title: string; entry: string; entryPath?: string; placements: string[]; operation?: string | null; permissions: string[]; sdkVersion?: number; requiredCapabilities?: string[]; category?: string; icon?: string; pluginId?: string; revision?: string }[]
  }
}
export type PluginUpdate = {
  status?: Exclude<PluginUpdateState, 'unchecked'>
  checked_at?: string
  target?: Record<string, unknown>
  local_version?: string | null
  remote_version?: string | null
  up_to_date?: boolean
  error?: string
  content_changed?: boolean
  local_commit?: string | null
  remote_commit?: string | null
}
export type PluginInventory = {
  installed: InstalledPlugin[]
  pluginsDir: string
  verificationError?: string | null
  recoveryRequired?: boolean
  runtime: { identity: string; pythonPath: string; bindingDir?: string; managementSupported?: boolean; updateSourceIdentity?: string }
  preinstalled?: { name: string; version: string; installed: boolean }[]
  preinstalledPending?: string[]
  preinstalledAvailable?: string[]
  inventoryWarnings?: string[]
  legacyPlugins?: string[]
}
export type PluginMarketplace = PluginInventory & {
  catalog: CatalogPlugin[]
  catalogError: string | null
  catalogSource?: {
    supported: boolean
    provider?: 'github' | 'gitcode' | 'custom'
    preference?: 'auto' | 'github' | 'gitcode'
    gitAvailable?: boolean
    httpProviders?: ('github' | 'gitcode')[]
    commit?: string
  }
}
export type PluginAction = 'install' | 'update' | 'repair' | 'uninstall' | 'check_updates' | 'enable' | 'disable' | 'prepare' | 'prepare_preinstalled' | 'install_preinstalled'
export const pluginPhases = ['queued', 'starting', 'working', 'preparing', 'checking', 'acquiring', 'preserving', 'resolving', 'installing', 'verifying', 'activating', 'refreshing', 'removing'] as const
export type PluginPhase = typeof pluginPhases[number]
export type PluginActivity = {
  id?: string
  batchId?: string
  runtime?: string
  action: PluginAction
  name: string
  phase: PluginPhase
  status: 'running' | 'cancelling' | 'succeeded' | 'failed' | 'cancelled'
  error: string
  startedAt: number
  updatedAt: number
  finishedAt: number | null
}
export function isPluginCommitPhase(phase: PluginPhase) {
  return phase === 'activating' || phase === 'refreshing' || phase === 'removing'
}
type WorkerEvent = {
  type: string
  taskId?: string
  payload: Record<string, unknown>
}

export const pluginRevision = ref(0)
const snapshot = ref<PluginMarketplace | null>(null)
const inventoryRequested = ref(false)
const updates = ref<Record<string, PluginUpdate>>({})
const loading = ref(false)
const catalogLoading = ref(false)
const error = ref('')
const activities = ref<PluginActivity[]>([])
const running = (task: PluginActivity) => task.status === 'running' || task.status === 'cancelling'
export type PluginUpdateBatchItem = PluginUpdateSelection & {
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'skipped' | 'cancelled'
  error: string
  taskId?: string
}
export type PluginUpdateBatch = {
  id: string
  runtime: PluginInventory['runtime']
  phase: 'running' | 'stopping' | 'finished'
  items: PluginUpdateBatchItem[]
}
const updateBatch = ref<PluginUpdateBatch | null>(null)
const batchBusy = computed(() => !!updateBatch.value && updateBatch.value.phase !== 'finished')
const taskBusy = computed(() => activities.value.some(running))
const recoveringBinding = ref(false)
const busy = computed(() => batchBusy.value || taskBusy.value || recoveringBinding.value)
const exclusiveBusy = computed(() => batchBusy.value || recoveringBinding.value || activities.value.some(task => running(task) && task.action !== 'install'))
let batchCompletion: Promise<void> | undefined
const logRecords = ref<LogRecord[]>([])
const logs = computed(() => logRecords.value.map(record => record.message))
const activity = computed(() => activities.value.at(-1) || null)
let sequence = 0
let catalogSequence = 0
let runtimeGeneration = 0
let inventoryRequest: Promise<void> | undefined
let runtimeReload: Promise<void> | undefined
type RuntimeReloadOptions = { includeCatalog?: boolean }
let deferredRuntimeReload: RuntimeReloadOptions | undefined
type BindingRecovery = { runtime: string; generation: number }
let recoveryPending: BindingRecovery | undefined
let recoveryAwaitingRelease: BindingRecovery | undefined
let bindingRecoveryRequest: Promise<void> | undefined
let catalogRequest: { generation: number; promise: Promise<void> } | undefined
let unlisten: UnlistenFn | undefined
let listening: Promise<void> | undefined
type ManageOptions = { name?: string; target?: string; locale?: string; mirror?: string; expectedSource?: string; expectedRuntime?: string; expectedUpdate?: PluginUpdate; installDependencies?: boolean; sourceRuntime?: string; sourceVersion?: string | null; sourceContent?: string | null }
type TaskControl = {
  id: string
  workerId: string
  action: PluginAction
  options: ManageOptions
  runtime?: string
  runtimeGeneration: number
  acceptedIds: Set<string>
  awaitingAcknowledgement: boolean
  dispatched: boolean
  invoked: boolean
  workerObserved: boolean
  completion: Promise<boolean>
  resolve: (receivedInventory: boolean) => void
  reject: (error: Error) => void
}
const tasks = new Map<string, TaskControl>()
const MAX_INSTALLS = 3
let closing = false
let errorOwner = ''
let earlyEvents: WorkerEvent[] = []
const earlyTerminals = new Map<string, WorkerEvent>()

function appendLog(message: string, context: Record<string, unknown> = {}, level?: 'info' | 'error') {
  if (!message.trim()) return
  const retained = new Set(logRecords.value.filter(record => record.context.pluginTaskId === context.pluginTaskId).slice(-159))
  logRecords.value = [...logRecords.value.filter(record => record.context.pluginTaskId !== context.pluginTaskId || retained.has(record)),
    { ...localLogRecord(message, 'plugin.activity', level || getLogLineLevel(message) || 'info'), logger: 'studio.plugins', context }]
}

function taskActivity(control: TaskControl) {
  return activities.value.find(task => task.id === control.id)!
}

function updateActivity(control: TaskControl, patch: Partial<PluginActivity>) {
  activities.value = activities.value.map(task => task.id === control.id ? { ...task, ...patch } : task)
}

function taskError(control: TaskControl, message: string) {
  if (control.runtimeGeneration !== runtimeGeneration) return
  errorOwner = control.id
  error.value = message
}

function applySnapshot(next: PluginMarketplace) {
  const previous = snapshot.value
  const before = new Map((previous?.installed || []).filter(plugin => plugin.managed).map(plugin => [plugin.name, pluginUpdateKey(plugin, previous!.runtime)]))
  const after = new Map(next.installed.filter(plugin => plugin.managed).map(plugin => [plugin.name, pluginUpdateKey(plugin, next.runtime)]))
  updates.value = Object.fromEntries(Object.entries(updates.value).filter(([name]) => before.has(name) && before.get(name) === after.get(name)))
  snapshot.value = next
  if (!next.recoveryRequired) {
    recoveryPending = undefined
    recoveryAwaitingRelease = undefined
  }
  else if (recoveryPending?.runtime !== next.runtime.identity || recoveryPending.generation !== runtimeGeneration) {
    recoveryPending = { runtime: next.runtime.identity, generation: runtimeGeneration }
    recoveryAwaitingRelease = undefined
  }
}

function recoverPendingBinding(batchId?: string): Promise<void> {
  if (bindingRecoveryRequest) return bindingRecoveryRequest
  const pending = recoveryPending
  if (!pending || closing || taskBusy.value || (batchBusy.value && batchId !== updateBatch.value?.id)) return Promise.resolve()
  const current = () => !closing && recoveryPending === pending && pending.generation === runtimeGeneration
    && snapshot.value?.runtime.identity === pending.runtime
  recoveryAwaitingRelease = undefined
  recoveringBinding.value = true
  const request = (async () => {
    try {
      await start()
      if (!current()) return
      const inventory = await retryRuntimeQuery(
        () => invoke<PluginInventory>('plugin_marketplace', { payload: {
          includeCatalog: false, recoverBinding: true, expectedRuntime: pending.runtime,
          defaultPolicy: useSettingsStore().pluginDefaultsPolicy,
        } }),
        { shouldRetry: failure => current() && isRuntimeAdmissionBusyError(failure) },
      )
      if (!current() || inventory.runtime.identity !== pending.runtime) return
      applySnapshot({ ...snapshot.value!, ...inventory, catalog: snapshot.value!.catalog,
        catalogError: snapshot.value!.catalogError, catalogSource: snapshot.value!.catalogSource,
        recoveryRequired: inventory.recoveryRequired === true })
      if (!inventory.recoveryRequired) pluginRevision.value += 1
    } catch (failure) {
      if (current()) {
        if (isRuntimeAdmissionBusyError(failure)) recoveryAwaitingRelease = pending
        else error.value = String(failure)
      }
    }
  })().finally(() => {
    if (bindingRecoveryRequest === request) {
      bindingRecoveryRequest = undefined
      recoveringBinding.value = false
    }
  })
  bindingRecoveryRequest = request
  return request
}

function scheduleBindingRecovery() {
  const pending = recoveryPending
  if (!pending || closing) return
  queueMicrotask(() => {
    const recover = () => {
      if (recoveryPending === pending && recoveryAwaitingRelease === pending) void recoverPendingBinding()
    }
    // A busy response can arrive after the competing operation released its lease.
    // Preserve that wakeup until the current recovery attempt has settled.
    if (bindingRecoveryRequest) void bindingRecoveryRequest.then(recover)
    else recover()
  })
}

function clearUpdateChecks() { updates.value = {} }

function finish(control: TaskControl, failure?: string, receivedInventory = false, cancelled = false) {
  if (!tasks.delete(control.id)) return
  earlyEvents = earlyEvents.filter(event => !event.taskId || !control.acceptedIds.has(event.taskId))
  for (const id of control.acceptedIds) earlyTerminals.delete(id)
  if (!tasks.size) { earlyEvents = []; earlyTerminals.clear() }
  updateActivity(control, { finishedAt: Date.now(),
    status: cancelled ? 'cancelled' : failure ? 'failed' : 'succeeded', error: cancelled ? '' : failure || '' })
  if ((!failure || cancelled) && (!errorOwner || errorOwner === control.id)) { error.value = ''; errorOwner = '' }
  if (!failure || cancelled) appendLog(cancelled ? 'Plugin operation cancelled' : 'Plugin operation completed', {
    pluginTaskId: control.id, pluginOutcome: cancelled ? 'cancelled' : 'succeeded' })
  if (failure) {
    if (!cancelled && control.action === 'update' && control.options.expectedUpdate && control.options.name) {
      delete updates.value[control.options.name]
    }
    if (!cancelled && control.action === 'check_updates') {
      const failed = (snapshot.value?.installed || []).filter(plugin => canCheckPluginUpdate(plugin) && (!control.options.name || control.options.name === plugin.name))
      const checked_at = new Date().toISOString()
      updates.value = { ...updates.value, ...Object.fromEntries(failed.map(plugin => [plugin.name, { status: 'error' as const, error: failure, checked_at }])) }
    }
    if (!cancelled) taskError(control, failure)
    if (!cancelled) appendLog(failure, { pluginTaskId: control.id }, 'error')
    control.reject(new Error(failure))
  } else {
    control.resolve(receivedInventory)
  }
  pump()
}

async function start() {
  if (unlisten) return
  if (!listening) {
    listening = listen<WorkerEvent>('pymss://worker-event', ({ payload: event }) => {
      handleEvent(event)
    }).then((callback) => { unlisten = callback }).finally(() => { listening = undefined })
  }
  await listening
}

function handleEvent(event: WorkerEvent) {
      if (recoveryPending && (event.type.endsWith('_done') || event.type.endsWith('_finished') || event.type.endsWith('_result')
          || ['runtime_access_released', 'env_info', 'runtime_info', 'runtime_env_sizes', 'task_cancelled'].includes(event.type)
          || event.type === 'error' && Boolean(event.taskId))) {
        scheduleBindingRecovery()
      }
      const control = [...tasks.values()].find(task => event.taskId && task.acceptedIds.has(event.taskId))
      if (!control) {
        if ([...tasks.values()].some(task => task.awaitingAcknowledgement)) {
          if (event.taskId && ['plugin_manage_result', 'error', 'task_cancelled'].includes(event.type)) {
            earlyTerminals.set(event.taskId, event)
            if (earlyTerminals.size > 32) earlyTerminals.delete(earlyTerminals.keys().next().value!)
          } else {
            earlyEvents = [...earlyEvents, event].slice(-160)
          }
        }
        return
      }
      control.workerObserved = true
      if (event.type === 'plugin_stage' || event.type === 'plugin_log') {
          const phase = event.payload.phase
          updateActivity(control, { updatedAt: Date.now(),
            ...(event.type === 'plugin_stage' ? {
              phase: pluginPhases.includes(phase as PluginPhase) ? phase as PluginPhase : 'working' } : {}) })
        appendLog(String(event.payload.message || ''), event.type === 'plugin_stage'
          ? { pluginTaskId: control.id, pluginPhase: event.payload.phase, pluginName: event.payload.pluginName }
          : { pluginTaskId: control.id })
      } else if (event.type === 'plugin_manage_result') {
        const inventory = event.payload as unknown as PluginInventory & { updates: Record<string, PluginUpdate> }
        if (control.runtimeGeneration === runtimeGeneration && (!control.runtime || inventory.runtime.identity === control.runtime) &&
            (!snapshot.value || snapshot.value.runtime.identity === inventory.runtime.identity)) {
          // Installations only add rows. A delayed snapshot must not erase a sibling's result.
          const installed = control.action === 'install'
            ? [...new Map([...(snapshot.value?.installed || []), ...inventory.installed].map(plugin => [installedPluginKey(plugin), plugin])).values()]
            : inventory.installed
          applySnapshot({ catalog: [], catalogError: null, ...snapshot.value, ...inventory, installed,
            recoveryRequired: inventory.recoveryRequired === true })
          if (control.action === 'check_updates') {
            const checked_at = new Date().toISOString()
            updates.value = { ...updates.value, ...Object.fromEntries(Object.entries(inventory.updates || {}).map(([name, result]) => [name, { checked_at, ...result }])) }
          }
          pluginRevision.value += 1
          finish(control, undefined, true)
          if (recoveryPending) queueMicrotask(() => { void recoverPendingBinding() })
        } else finish(control)
      } else if (event.type === 'error') {
        finish(control, String(event.payload.message || 'Plugin operation failed'))
      } else if (event.type === 'task_cancelled') {
        // The next refresh recovers an interrupted publication before querying plugins.
        finish(control, 'Plugin operation cancelled', false, true)
      }
}

async function loadSnapshot(includeCatalog: boolean, prepare = true, forceVerify = false, batchId?: string) {
  if (closing || taskBusy.value || recoveringBinding.value || (batchBusy.value && batchId !== updateBatch.value?.id)) return
  inventoryRequested.value = true
  const request = ++sequence
  loading.value = true
  error.value = ''
  try {
    const settings = useSettingsStore()
    await settings.initialize()
    const read = () => invoke<PluginMarketplace>('plugin_marketplace', { payload: {
      includeCatalog, defaultPolicy: settings.pluginDefaultsPolicy,
      ...(forceVerify ? { forceVerify: true } : {}),
    } })
    const result = forceVerify ? await read() : await retryRuntimeQuery(read, {
      shouldRetry: () => !closing && request === sequence,
    })
    if (request !== sequence) return
    const runtimeChanged = snapshot.value?.runtime.identity !== result.runtime.identity
    if (runtimeChanged) updates.value = {}
    applySnapshot(!includeCatalog && !runtimeChanged && snapshot.value
      ? { ...result, catalog: snapshot.value.catalog, catalogError: snapshot.value.catalogError, catalogSource: snapshot.value.catalogSource }
      : result)
    if (runtimeChanged) pluginRevision.value += 1
    if (recoveryPending) {
      await start()
      if (!forceVerify) await recoverPendingBinding(batchId)
    }
    if (prepare && !snapshot.value?.recoveryRequired && snapshot.value?.preinstalledPending?.length) {
      loading.value = false
      await manage('prepare_preinstalled')
    }
  } catch (failure) {
    if (request === sequence) {
      error.value = String(failure)
    }
  } finally {
    if (request === sequence) loading.value = false
  }
}

function refresh(forceVerify = false, options: { prepare?: boolean } = {}) { return runtimeReload || loadSnapshot(true, options.prepare !== false, forceVerify === true) }
function refreshInventory(forceVerify = false) { return runtimeReload || loadSnapshot(false, true, forceVerify === true) }

function reloadRuntimeInventory(options: RuntimeReloadOptions = {}) {
  if (closing) { deferredRuntimeReload = options; return Promise.resolve() }
  deferredRuntimeReload = undefined
  inventoryRequested.value = true
  const generation = ++runtimeGeneration
  recoveryPending = undefined
  recoveryAwaitingRelease = undefined
  const pendingRecovery = bindingRecoveryRequest
  ++sequence
  ++catalogSequence
  snapshot.value = null
  updates.value = {}
  error.value = ''
  errorOwner = ''
  loading.value = true
  catalogLoading.value = false
  inventoryRequest = undefined
  catalogRequest = undefined
  stopBatchUpdates()
  const pending = [...tasks.values()].map(task => task.completion.catch(() => undefined))
  const pendingBatch = batchCompletion
  const reload = (async () => {
    await Promise.all(pending)
    await pendingBatch
    await pendingRecovery
    if (generation !== runtimeGeneration) return
    if (closing) { deferredRuntimeReload ||= options; return }
    await loadSnapshot(false)
    if (generation !== runtimeGeneration) return
    if (closing) deferredRuntimeReload ||= options
    else if (options.includeCatalog) void ensureCatalog()
  })()
  runtimeReload = reload
  void reload.finally(() => {
    if (runtimeReload === reload) { runtimeReload = undefined; loading.value = false }
  }).catch(() => undefined)
  return reload
}

function ensureInventory() {
  if (runtimeReload) return runtimeReload
  if (!inventoryRequest) {
    const request = loadSnapshot(false).finally(() => { if (inventoryRequest === request) inventoryRequest = undefined })
    inventoryRequest = request
  }
  return inventoryRequest
}

async function openMarketplace(forceInventory = false) {
  await (forceInventory ? refreshInventory() : ensureInventory())
  return ensureCatalog()
}

function ensureCatalog() {
  if (!snapshot.value || busy.value) return
  if (catalogRequest?.generation === sequence) return catalogRequest.promise
  const promise = loadCatalog()
  catalogRequest = { generation: sequence, promise }
  return promise.finally(() => { if (catalogRequest?.promise === promise) catalogRequest = undefined })
}

async function loadCatalog() {
  if (!snapshot.value) return
  const request = ++catalogSequence
  const generation = sequence
  const runtime = snapshot.value.runtime.identity
  catalogLoading.value = true
  try {
    const catalog = await retryRuntimeQuery(
      () => invoke<Pick<PluginMarketplace, 'catalog' | 'catalogError' | 'catalogSource' | 'runtime'>>('plugin_marketplace', { payload: { catalogOnly: true } }),
      { shouldRetry: () => !closing && request === catalogSequence && generation === sequence },
    )
    if (request !== catalogSequence || generation !== sequence || snapshot.value?.runtime.identity !== runtime || catalog.runtime.identity !== runtime) return
    applySnapshot({ ...snapshot.value, catalog: catalog.catalog, catalogError: catalog.catalogError, catalogSource: catalog.catalogSource })
  } catch (failure) {
    if (request === catalogSequence && generation === sequence && snapshot.value?.runtime.identity === runtime) {
      snapshot.value = { ...snapshot.value, catalog: [], catalogError: String(failure) }
    }
  } finally {
    if (request === catalogSequence) catalogLoading.value = false
  }
}

function pump() {
  if (closing) return
  let active = [...tasks.values()].filter(task => task.dispatched).length
  for (const control of tasks.values()) {
    if (control.dispatched) continue
    if (active >= MAX_INSTALLS) break
    control.dispatched = true
    active++
    updateActivity(control, { phase: 'starting' })
    void dispatch(control)
  }
}

async function dispatch(control: TaskControl) {
  try {
    await start()
    if (!tasks.has(control.id)) return
    if (control.runtimeGeneration !== runtimeGeneration) {
      finish(control, 'Plugin operation cancelled', false, true)
      return
    }
    control.invoked = true
    control.awaitingAcknowledgement = true
    const response = await invoke<{ taskId: string }>('manage_plugin', {
      payload: { action: control.action, ...control.options, taskId: control.id, defaultPolicy: useSettingsStore().pluginDefaultsPolicy },
    })
    if (!tasks.has(control.id)) return
    control.workerId = response.taskId || control.id
    control.acceptedIds.add(control.workerId)
    control.awaitingAcknowledgement = false
    const queued = [...earlyEvents, ...earlyTerminals.values()].filter(event => event.taskId && control.acceptedIds.has(event.taskId))
    earlyEvents = earlyEvents.filter(event => !event.taskId || !control.acceptedIds.has(event.taskId))
    for (const id of control.acceptedIds) earlyTerminals.delete(id)
    for (const event of queued) handleEvent(event)
    if (tasks.has(control.id) && taskActivity(control).status === 'cancelling') {
      try { await cancelWorker(control) }
      catch (failure) { taskError(control, String(failure)); appendLog(error.value, { pluginTaskId: control.id }) }
    }
  } catch (failure) {
    finish(control, String(failure))
  }
}

async function manageTask(action: PluginAction, options: ManageOptions = {}, batchId?: string) {
  const name = options.name || options.target || ''
  if (closing || (batchBusy.value && batchId !== updateBatch.value?.id)
      || activities.value.some(task => running(task) && task.action !== 'install')
      || loading.value || catalogLoading.value || recoveringBinding.value || (action !== 'install' && taskBusy.value)) {
    throw new Error('Plugin management is already running')
  }
  if (activities.value.some(task => running(task) && task.name === name)) throw new Error('This plugin already has an active task')
  const generation = runtimeGeneration
  ++sequence
  let receivedInventory = false
  error.value = ''
  errorOwner = ''
  const now = Date.now()
  const id = `plugin_manage_${now}_${Math.random().toString(36).slice(2, 10)}`
  let resolve!: TaskControl['resolve'], reject!: TaskControl['reject']
  const completion = new Promise<boolean>((success, failure) => { resolve = success; reject = failure })
  void completion.catch(() => undefined)
  activities.value = [...activities.value.filter(task => running(task)).concat(activities.value.filter(task => !running(task)).slice(-19)),
    { id, batchId, action, name, runtime: snapshot.value?.runtime.identity, phase: 'queued', status: 'running', error: '', startedAt: now, updatedAt: now, finishedAt: null }]
  const retainedIds = new Set(activities.value.map(task => task.id))
  logRecords.value = logRecords.value.filter(record => retainedIds.has(record.context.pluginTaskId as string))
  const control: TaskControl = { id, workerId: id, action, options, runtime: snapshot.value?.runtime.identity, runtimeGeneration: generation,
    acceptedIds: new Set([id]), awaitingAcknowledgement: false, dispatched: false, invoked: false, workerObserved: false, completion, resolve, reject }
  tasks.set(id, control)
  pump()
  try {
    receivedInventory = await completion
  } finally {
    // Failed imports and cancelled tasks may still have changed on-disk state.
    const operationError = error.value
    if (generation === runtimeGeneration && !receivedInventory) {
      const recoverBinding = action !== 'check_updates' && control.invoked && (control.workerObserved || !control.awaitingAcknowledgement)
      if (recoverBinding && control.runtime && snapshot.value?.runtime.identity === control.runtime) {
        const pending = { runtime: control.runtime, generation }
        recoveryPending = pending
        recoveryAwaitingRelease = undefined
        if (!taskBusy.value) {
          await bindingRecoveryRequest
          if (recoveryPending === pending) await recoverPendingBinding(batchId)
        }
      } else if (!taskBusy.value) await loadSnapshot(false, false, false, batchId)
    }
    if (generation === runtimeGeneration && operationError) error.value = operationError
  }
}

async function cancel(id = activity.value?.id) {
  const control = id ? tasks.get(id) : undefined
  if (!control) return
  const task = taskActivity(control)
  if (task.status === 'cancelling' || isPluginCommitPhase(task.phase)) return
  if (!control.invoked) { finish(control, 'Plugin operation cancelled', false, true); return }
  updateActivity(control, { status: 'cancelling' })
  await cancelWorker(control)
}

function manage(action: PluginAction, options: ManageOptions = {}) { return manageTask(action, options) }

function stopBatchUpdates() {
  if (batchBusy.value) updateBatch.value!.phase = 'stopping'
}

function updatePlugins(selection: PluginUpdateSelection[], options: { installDependencies?: boolean; locale?: string } = {}) {
  if (closing || busy.value || loading.value || catalogLoading.value) return Promise.reject(new Error('Plugin management is already running'))
  if (!selection.length || !snapshot.value || snapshot.value.runtime.managementSupported === false) return Promise.reject(new Error('No reviewed plugin updates are available'))
  const id = `plugin_updates_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`
  const batch: PluginUpdateBatch = { id, phase: 'running', runtime: { ...snapshot.value.runtime },
    items: JSON.parse(JSON.stringify(selection)).map((item: PluginUpdateSelection) => ({ ...item, status: 'queued', error: '' })) }
  updateBatch.value = batch
  // Use the reactive instance so item changes remain visible across navigation.
  const current = updateBatch.value!
  batchCompletion = (async () => {
    const seen = new Set<string>()
    try {
      for (const item of current.items) {
        if (current.phase === 'stopping' || closing) { item.status = 'cancelled'; continue }
        const inventory = snapshot.value
        const plugin = inventory?.installed.find(plugin => plugin.managed && plugin.name === item.name)
        if (!plugin || !inventory || seen.has(item.name) || !canCheckPluginUpdate(plugin)
            || !canApplyPluginUpdate(item.update) || pluginUpdateKey(plugin, inventory.runtime) !== item.installationKey) {
          item.status = 'skipped'
          continue
        }
        seen.add(item.name)
        item.status = 'running'
        const pending = manageTask('update', { name: item.name, expectedRuntime: inventory.runtime.identity,
          expectedUpdate: item.update, installDependencies: options.installDependencies === true, locale: options.locale }, id)
        item.taskId = activities.value.find(task => task.batchId === id && task.name === item.name)?.id
        try {
          await pending
          item.status = 'succeeded'
        } catch (failure) {
          const task = activities.value.find(task => task.id === item.taskId)
          item.status = task?.status === 'cancelled' ? 'cancelled' : 'failed'
          item.error = task?.error || String(failure)
          if (item.status === 'cancelled') stopBatchUpdates()
        }
      }
    } finally {
      current.phase = 'finished'
    }
  })()
  const completion = batchCompletion
  void completion.finally(() => { if (batchCompletion === completion) batchCompletion = undefined }).catch(() => undefined)
  return completion
}

async function cancelWorker(control: TaskControl) {
  if (isPluginCommitPhase(taskActivity(control).phase)) {
    updateActivity(control, { status: 'running' })
    return
  }
  try {
    // A false result can mean the terminal event is already queued for delivery.
    await invoke<boolean>('cancel_task', { taskId: control.workerId })
  } catch (failure) {
    if (tasks.has(control.id)) updateActivity(control, { status: 'running' })
    throw failure
  }
}
registerWindowCloseGuard(async () => {
  if (!busy.value && !runtimeReload) return
  closing = true
  recoveryAwaitingRelease = undefined
  stopBatchUpdates()
  const pendingBatch = batchCompletion
  const pendingReload = runtimeReload
  const pendingRecovery = bindingRecoveryRequest
  try {
    const pending = [...tasks.values()]
    await Promise.all(pending.map(task => cancel(task.id)))
    await Promise.all(pending.map(task => task.completion.catch(() => undefined)))
    await pendingBatch
    await pendingReload
    await pendingRecovery
  } finally { closing = false; pump() }
}, 70, () => {
  if (deferredRuntimeReload) void reloadRuntimeInventory(deferredRuntimeReload)
  else if (inventoryRequested.value) void loadSnapshot(false, false)
})

export function usePluginMarketplace() {
  return { snapshot, inventoryRequested, updates, loading, catalogLoading, error, busy, exclusiveBusy, logs, logRecords, activity, activities,
    updateBatch, batchBusy, updatePlugins, stopBatchUpdates,
    ready: computed(() => snapshot.value !== null), refresh, refreshInventory, reloadRuntimeInventory, ensureInventory, openMarketplace, clearUpdateChecks, manage, cancel }
}
