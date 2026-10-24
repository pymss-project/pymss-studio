/**
 * Plugin-operation runtime bridge (round 5 of the pymss plugin redesign).
 *
 * Discovery uses one-shot worker commands (list_operations /
 * describe_operation). Execution runs as a background task through
 * `start_operation_task`; the terminal `operation_result` / `error` /
 * `task_cancelled` worker events resolve the pending promise.
 */
import { reactive, ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'

export type OperationParamSchema = {
  type: 'number' | 'integer' | 'string' | 'boolean'
  default?: unknown
  minimum?: number
  maximum?: number
  enum?: (string | number)[]
  description?: string
}

export type OperationSpec = {
  id: string
  schemaVersion: number
  title: string
  description?: string
  inputs: Record<string, { type: string }>
  params: {
    type: 'object'
    properties: Record<string, OperationParamSchema>
    required?: string[]
    additionalProperties?: boolean
  }
  outputs: Record<string, { type: string }>
}

export type OperationRunResult = {
  operation: string
  outputs: Record<string, { kind: string; path?: string; value?: unknown; sampleRate?: number }>
  outputDir: string
}

export type OperationProgress = { fraction: number; message: string }

type WorkerEvent = {
  requestId?: string
  taskId?: string
  type: string
  payload?: Record<string, unknown>
}

const state = reactive({
  available: false,
  unavailableReason: '' as string | undefined,
  specs: [] as OperationSpec[],
})
const loading = ref(false)
const error = ref<string | null>(null)

let unlistenWorker: UnlistenFn | undefined
let startInFlight: Promise<void> | undefined
let pendingRun: { id: string; resolve: (r: OperationRunResult) => void; reject: (e: Error) => void } | undefined
const progressHandlers = new Map<string, (p: OperationProgress) => void>()

async function start() {
  if (typeof window === 'undefined' || !('__TAURI_INTERNALS__' in window)) return
  if (unlistenWorker) return
  if (!startInFlight) {
    startInFlight = listen<WorkerEvent>('pymss://worker-event', (event) => {
      const workerEvent = event.payload
      const eventId = workerEvent.requestId || workerEvent.taskId
      const pending = pendingRun
      if (!pending || eventId !== pending.id) return
      if (workerEvent.type === 'operation_result') {
        pendingRun = undefined
        pending.resolve(workerEvent.payload as unknown as OperationRunResult)
      } else if (workerEvent.type === 'error') {
        pendingRun = undefined
        pending.reject(new Error(String(workerEvent.payload?.message || 'Operation failed')))
      } else if (workerEvent.type === 'task_cancelled') {
        pendingRun = undefined
        pending.reject(new Error('cancelled'))
      } else if (workerEvent.type === 'operation_progress') {
        progressHandlers.get(pending.id)?.({
          fraction: Number(workerEvent.payload?.fraction || 0),
          message: String(workerEvent.payload?.message || ''),
        })
      }
    }).then((unlisten) => {
      unlistenWorker = unlisten
    }).finally(() => {
      startInFlight = undefined
    })
  }
  await startInFlight
}

let requestSequence = 0

export function usePluginOps() {
  async function refresh() {
    loading.value = true
    error.value = null
    try {
      const response = await invoke<{
        operations: OperationSpec[]
        pymssOperationsAvailable: boolean
        error?: string
      }>('list_operations')
      state.available = response.pymssOperationsAvailable !== false
      state.unavailableReason = response.error
      state.specs = response.operations || []
    } catch (err) {
      state.available = false
      state.specs = []
      error.value = err instanceof Error ? err.message : String(err)
    } finally {
      loading.value = false
    }
  }

  async function describe(operationId: string): Promise<OperationSpec | null> {
    try {
      return await invoke<OperationSpec>('describe_operation', { payload: { operation: operationId } })
    } catch {
      return null
    }
  }

  async function run(options: {
    operation: string
    params: Record<string, unknown>
    inputs: Record<string, { kind: 'file'; path: string }>
    outputDir?: string
    onProgress?: (p: OperationProgress) => void
  }): Promise<OperationRunResult> {
    await start()
    const requestId = `operation_${options.operation}_${Date.now()}_${++requestSequence}`
    const completion = new Promise<OperationRunResult>((resolve, reject) => {
      pendingRun = { id: requestId, resolve, reject }
    })
    if (options.onProgress) progressHandlers.set(requestId, options.onProgress)
    try {
      await invoke('start_operation_task', {
        payload: {
          operation: options.operation,
          params: options.params,
          inputs: options.inputs,
          outputDir: options.outputDir,
          requestId,
        },
      })
      return await completion
    } finally {
      progressHandlers.delete(requestId)
      if (pendingRun?.id === requestId) pendingRun = undefined
    }
  }

  return { state, loading, error, refresh, describe, run }
}
