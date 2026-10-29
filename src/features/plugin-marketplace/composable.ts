import { computed, ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { registerWindowCloseGuard } from '@/utils/windowCloseGuards'
import { localLogRecord, type LogRecord } from '@/utils/logRecord'
import { getLogLineLevel } from '@/utils/taskLog'
import type { PluginCategory, PluginLocales } from './presentation'

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
  operations: string[] | null
  operationTitles?: Record<string, string>
  description?: string
  locales?: PluginLocales
  enabled?: boolean
  preinstalled?: boolean
  toolCategory?: 'convert' | 'analyze' | 'recognize' | 'edit' | null
  toolIcon?: string | null
  dependencyError?: string | null
  requirements?: PluginRequirements | null
  compatibility?: PluginCompatibility
  resolvedCommit?: string | null
  contributions?: {
    graphs?: { id: string; file: string; operation: string }[]
    views?: { id: string; title: string; entry: string; entryPath?: string; placements: string[]; operation?: string | null; permissions: string[]; sdkVersion?: number; requiredCapabilities?: string[]; category?: string; icon?: string; pluginId?: string; revision?: string }[]
  }
}
export type PluginUpdate = {
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
  runtime: { identity: string; pythonPath: string }
  preinstalled?: { name: string; version: string; installed: boolean }[]
  preinstalledPending?: string[]
  legacyPlugins?: string[]
}
export type PluginMarketplace = PluginInventory & {
  catalog: CatalogPlugin[]
  catalogError: string | null
}
export type PluginAction = 'install' | 'update' | 'repair' | 'uninstall' | 'check_updates' | 'enable' | 'disable' | 'prepare_preinstalled' | 'install_preinstalled'
export const pluginPhases = ['queued', 'starting', 'working', 'preparing', 'checking', 'acquiring', 'preserving', 'resolving', 'installing', 'verifying', 'activating', 'refreshing', 'removing'] as const
export type PluginPhase = typeof pluginPhases[number]
export type PluginActivity = {
  id?: string
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
const updates = ref<Record<string, PluginUpdate>>({})
const loading = ref(false)
const catalogLoading = ref(false)
const error = ref('')
const activities = ref<PluginActivity[]>([])
const running = (task: PluginActivity) => task.status === 'running' || task.status === 'cancelling'
const busy = computed(() => activities.value.some(running))
const exclusiveBusy = computed(() => activities.value.some(task => running(task) && task.action !== 'install'))
const logRecords = ref<LogRecord[]>([])
const logs = computed(() => logRecords.value.map(record => record.message))
const activity = computed(() => activities.value.at(-1) || null)
let sequence = 0
let catalogSequence = 0
let inventoryRequest: Promise<void> | undefined
let catalogRequest: { generation: number; promise: Promise<void> } | undefined
let unlisten: UnlistenFn | undefined
let listening: Promise<void> | undefined
type ManageOptions = { name?: string; target?: string; locale?: string; mirror?: string; expectedSource?: string; expectedRuntime?: string; installDependencies?: boolean }
type TaskControl = {
  id: string
  workerId: string
  action: PluginAction
  options: ManageOptions
  runtime?: string
  acceptedIds: Set<string>
  awaitingAcknowledgement: boolean
  dispatched: boolean
  invoked: boolean
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
  if (message.trim()) logRecords.value = [...logRecords.value,
    { ...localLogRecord(message, 'plugin.activity', level || getLogLineLevel(message) || 'info'), logger: 'studio.plugins', context }].slice(-160)
}

function taskActivity(control: TaskControl) {
  return activities.value.find(task => task.id === control.id)!
}

function updateActivity(control: TaskControl, patch: Partial<PluginActivity>) {
  activities.value = activities.value.map(task => task.id === control.id ? { ...task, ...patch } : task)
}

function taskError(control: TaskControl, message: string) {
  errorOwner = control.id
  error.value = message
}

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
        if ((!control.runtime || inventory.runtime.identity === control.runtime) &&
            (!snapshot.value || snapshot.value.runtime.identity === inventory.runtime.identity)) {
          // Installations only add rows. A delayed snapshot must not erase a sibling's result.
          const installed = control.action === 'install'
            ? [...new Map([...(snapshot.value?.installed || []), ...inventory.installed].map(plugin => [`${plugin.managed}:${plugin.name}`, plugin])).values()]
            : inventory.installed
          snapshot.value = { catalog: [], catalogError: null, ...snapshot.value, ...inventory, installed }
          updates.value = { ...updates.value, ...inventory.updates }
          pluginRevision.value += 1
          finish(control, undefined, true)
        } else finish(control)
      } else if (event.type === 'error') {
        finish(control, String(event.payload.message || 'Plugin operation failed'))
      } else if (event.type === 'task_cancelled') {
        // The next refresh recovers an interrupted publication before querying plugins.
        finish(control, 'Plugin operation cancelled', false, true)
      }
}

async function loadSnapshot(includeCatalog: boolean, prepare = true, forceVerify = false) {
  if (busy.value) return
  const request = ++sequence
  loading.value = true
  error.value = ''
  try {
    const result = await invoke<PluginMarketplace>('plugin_marketplace', { payload: { includeCatalog, ...(forceVerify ? { forceVerify: true } : {}) } })
    if (request !== sequence) return
    const runtimeChanged = snapshot.value?.runtime.identity !== result.runtime.identity
    if (runtimeChanged) updates.value = {}
    snapshot.value = !includeCatalog && !runtimeChanged && snapshot.value
      ? { ...result, catalog: snapshot.value.catalog, catalogError: snapshot.value.catalogError }
      : result
    if (runtimeChanged) pluginRevision.value += 1
    if (prepare && result.preinstalledPending?.length) {
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

function refresh(forceVerify = false) { return loadSnapshot(true, true, forceVerify === true) }
function refreshInventory(forceVerify = false) { return loadSnapshot(false, true, forceVerify === true) }

function reloadRuntimeInventory() {
  snapshot.value = null
  updates.value = {}
  return refreshInventory()
}

function ensureInventory() {
  if (!inventoryRequest) {
    inventoryRequest = loadSnapshot(false).finally(() => { inventoryRequest = undefined })
  }
  return inventoryRequest
}

async function openMarketplace(forceInventory = false) {
  await (forceInventory ? refreshInventory() : ensureInventory())
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
    const catalog = await invoke<Pick<PluginMarketplace, 'catalog' | 'catalogError' | 'runtime'>>('plugin_marketplace', { payload: { catalogOnly: true } })
    if (request !== catalogSequence || generation !== sequence || snapshot.value?.runtime.identity !== runtime || catalog.runtime.identity !== runtime) return
    snapshot.value = { ...snapshot.value, catalog: catalog.catalog, catalogError: catalog.catalogError }
  } catch (failure) {
    if (request === catalogSequence && generation === sequence && snapshot.value?.runtime.identity === runtime) {
      snapshot.value = { ...snapshot.value, catalogError: String(failure) }
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
    control.invoked = true
    control.awaitingAcknowledgement = true
    const response = await invoke<{ taskId: string }>('manage_plugin', {
      payload: { action: control.action, ...control.options, taskId: control.id },
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

async function manage(action: PluginAction, options: ManageOptions = {}) {
  const name = options.name || options.target || ''
  if (closing || exclusiveBusy.value || loading.value || catalogLoading.value || (action !== 'install' && busy.value)) {
    throw new Error('Plugin management is already running')
  }
  if (activities.value.some(task => running(task) && task.name === name)) throw new Error('This plugin already has an active task')
  ++sequence
  let receivedInventory = false
  error.value = ''
  errorOwner = ''
  if (!busy.value) { logRecords.value = []; activities.value = [] }
  const now = Date.now()
  const id = `plugin_manage_${now}_${Math.random().toString(36).slice(2, 10)}`
  let resolve!: TaskControl['resolve'], reject!: TaskControl['reject']
  const completion = new Promise<boolean>((success, failure) => { resolve = success; reject = failure })
  void completion.catch(() => undefined)
  activities.value = [...activities.value.filter(task => running(task)).concat(activities.value.filter(task => !running(task)).slice(-19)),
    { id, action, name, phase: 'queued', status: 'running', error: '', startedAt: now, updatedAt: now, finishedAt: null }]
  tasks.set(id, { id, workerId: id, action, options, runtime: snapshot.value?.runtime.identity,
    acceptedIds: new Set([id]), awaitingAcknowledgement: false, dispatched: false, invoked: false, completion, resolve, reject })
  pump()
  try {
    receivedInventory = await completion
    if (action !== 'check_updates') {
      if (options.name) delete updates.value[options.name]
    }
  } finally {
    // Failed imports and cancelled tasks may still have changed on-disk state.
    const operationError = error.value
    if (!receivedInventory && !busy.value) await loadSnapshot(false, false)
    if (operationError) error.value = operationError
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
  if (!busy.value) return
  closing = true
  try {
    const pending = [...tasks.values()]
    await Promise.all(pending.map(task => cancel(task.id)))
    await Promise.all(pending.map(task => task.completion.catch(() => undefined)))
  } finally { closing = false; pump() }
}, 70)

export function usePluginMarketplace() {
  return { snapshot, updates, loading, catalogLoading, error, busy, exclusiveBusy, logs, logRecords, activity, activities,
    ready: computed(() => snapshot.value !== null), refresh, refreshInventory, reloadRuntimeInventory, ensureInventory, openMarketplace, manage, cancel }
}
