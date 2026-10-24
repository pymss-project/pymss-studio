import { computed, ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { registerWindowCloseGuard } from '@/utils/windowCloseGuards'

export type CatalogPlugin = {
  name: string
  url: string
  subpath?: string | null
  description: string
  tags: string[]
}
export type InstalledPlugin = {
  name: string
  version: string | null
  source: string
  url: string | null
  path: string
  loaded: boolean | null
  managed: boolean
  error: string | null
  operations: string[] | null
  contributions?: {
    graphs?: { id: string; file: string; operation: string }[]
    views?: { id: string; title: string; entry: string; entryPath?: string; placements: string[]; operation?: string | null; permissions: string[] }[]
    player?: { operation: string; scopes: string[]; modes: string[] }[]
  }
}
export type PluginUpdate = {
  local_version?: string | null
  remote_version?: string | null
  up_to_date?: boolean
  error?: string
}
export type PluginInventory = {
  installed: InstalledPlugin[]
  pluginsDir: string
  verificationError?: string | null
  runtime: { identity: string; pythonPath: string }
}
export type PluginMarketplace = PluginInventory & {
  catalog: CatalogPlugin[]
  catalogError: string | null
}
export type PluginAction = 'install' | 'update' | 'repair' | 'uninstall' | 'check_updates'
type WorkerEvent = {
  type: string
  taskId?: string
  payload: Record<string, unknown>
}

export const pluginRevision = ref(0)
const snapshot = ref<PluginMarketplace | null>(null)
const updates = ref<Record<string, PluginUpdate>>({})
const loading = ref(false)
const error = ref('')
const busy = ref(false)
const taskId = ref<string | null>(null)
const logs = ref<string[]>([])
const currentAction = ref<PluginAction | null>(null)
const currentName = ref('')
let sequence = 0
let unlisten: UnlistenFn | undefined
let listening: Promise<void> | undefined
let pending: { resolve: () => void; reject: (error: Error) => void } | undefined
let acceptedIds = new Set<string>()
let awaitingAcknowledgement = false
let earlyEvents: WorkerEvent[] = []
const earlyTerminals = new Map<string, WorkerEvent>()

function appendLog(message: string) {
  if (message.trim()) logs.value = [...logs.value, message].slice(-160)
}

function finish(failure?: string) {
  const completion = pending
  pending = undefined
  acceptedIds.clear()
  awaitingAcknowledgement = false
  earlyEvents = []
  earlyTerminals.clear()
  busy.value = false
  taskId.value = null
  currentAction.value = null
  currentName.value = ''
  if (failure) {
    error.value = failure
    appendLog(failure)
    completion?.reject(new Error(failure))
  } else {
    completion?.resolve()
  }
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
      if (!busy.value) return
      if (!event.taskId || !acceptedIds.has(event.taskId)) {
        if (awaitingAcknowledgement) {
          if (event.taskId && ['plugin_manage_result', 'error', 'task_cancelled'].includes(event.type)) {
            earlyTerminals.set(event.taskId, event)
          } else {
            earlyEvents = [...earlyEvents, event].slice(-160)
          }
        }
        return
      }
      if (event.type === 'plugin_stage' || event.type === 'plugin_log') {
        appendLog(String(event.payload.message || ''))
      } else if (event.type === 'plugin_manage_result') {
        const inventory = event.payload as unknown as PluginInventory & { updates: Record<string, PluginUpdate> }
        if (snapshot.value) snapshot.value = { ...snapshot.value, ...inventory }
        updates.value = { ...updates.value, ...inventory.updates }
        pluginRevision.value += 1
        finish()
      } else if (event.type === 'error') {
        finish(String(event.payload.message || 'Plugin operation failed'))
      } else if (event.type === 'task_cancelled') {
        // The next refresh recovers an interrupted publication before querying plugins.
        finish('Plugin operation cancelled')
      }
}

async function refresh() {
  if (busy.value) return
  const request = ++sequence
  loading.value = true
  error.value = ''
  try {
    const result = await invoke<PluginMarketplace>('plugin_marketplace')
    if (request !== sequence) return
    if (snapshot.value?.runtime.identity !== result.runtime.identity) updates.value = {}
    snapshot.value = result
  } catch (failure) {
    if (request === sequence) {
      error.value = String(failure)
    }
  } finally {
    if (request === sequence) loading.value = false
  }
}

async function manage(action: PluginAction, options: { name?: string; target?: string; locale?: string; mirror?: string } = {}) {
  if (busy.value || loading.value) throw new Error('Plugin management is already running')
  ++sequence
  busy.value = true
  error.value = ''
  currentAction.value = action
  currentName.value = options.name || options.target || ''
  logs.value = []
  taskId.value = `plugin_manage_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`
  const requestedId = taskId.value
  acceptedIds = new Set([requestedId])
  awaitingAcknowledgement = true
  const completion = new Promise<void>((resolve, reject) => { pending = { resolve, reject } })
  try {
    await start()
    const response = await invoke<{ taskId: string }>('manage_plugin', { payload: { action, ...options, taskId: requestedId } })
    if (busy.value && acceptedIds.has(requestedId)) {
      taskId.value = response.taskId || requestedId
      acceptedIds.add(taskId.value)
      awaitingAcknowledgement = false
      const queued = [...earlyEvents, ...earlyTerminals.values()]
      earlyEvents = []
      earlyTerminals.clear()
      for (const event of queued) handleEvent(event)
    }
  } catch (failure) {
    finish(String(failure))
  }
  try {
    await completion
    if (action !== 'check_updates') {
      if (options.name) delete updates.value[options.name]
    }
  } finally {
    // Failed imports and cancelled tasks may still have changed on-disk state.
    const operationError = error.value
    await refresh()
    if (operationError) error.value = operationError
  }
}

async function cancel() {
  if (!taskId.value) return
  const id = taskId.value
  // A false result can mean the terminal event is already queued for delivery.
  await invoke<boolean>('cancel_task', { taskId: id })
}
registerWindowCloseGuard(async () => { if (busy.value) await cancel() }, 70)

export function usePluginMarketplace() {
  return { snapshot, updates, loading, error, busy, logs, currentAction, currentName,
    ready: computed(() => snapshot.value !== null), refresh, manage, cancel }
}
