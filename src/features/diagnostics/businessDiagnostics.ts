import { ref, shallowRef } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import type { RenderDiagnosticState, RenderPreferences } from '@/utils/renderDiagnostics'

export type WindowDiagnosticState = {
  windowLabel: string; observedAt: string; requestId: string; kind: 'workflow' | 'editor'
  workflowId?: string; projectId?: string; updatedAt?: number; dirty?: boolean; saving?: boolean
  error?: string | null; revisionConflict?: boolean; recoveryBackupAvailable?: boolean
  loading?: boolean; exporting?: boolean; exportCancelling?: boolean; exportPhase?: string
  exportCompleted?: number; exportTotal?: number; hasLastExport?: boolean
  lastExportPath?: string
  playbackStatus?: string; playbackError?: string; recordingState?: string; recordingError?: string
  pluginStateDirty?: boolean; pluginStateError?: string | null; pluginStateUnavailable?: boolean
}
type NativeStatus = { status: string; error?: string; [key: string]: unknown }
export type BusinessDiagnosticSnapshot = {
  checkedAt: string
  requestId: string
  windows: { status: string; expected: string[]; observed: WindowDiagnosticState[]; missing: string[] }
  nativeHost: {
    capabilities: { enabled: boolean; platform: string; formats: string[]; nativeEditor: boolean; nativeTransport: boolean }
    stopping: boolean; session: NativeStatus; scan: NativeStatus; renders: NativeStatus
    storage: NativeStatus
  }
}
export type OutputDiagnostic = { path: string; state: 'present' | 'missing' | 'unreadable' | 'not_file' | 'unresolved'; bytes?: number; error?: string }
export type OutputDiagnosticSnapshot = { checkedAt: string; files: OutputDiagnostic[] }
export type RendererDiagnosticSnapshot = {
  checkedAt: string; requestId: string; webviewVersion: string | null; webviewError: string | null; os: string; arch: string
  windows: { status: string; expected: string[]; missing: string[]; observed: Array<{
    windowLabel: string; observedAt: string; kind: 'renderer'; rendering?: RenderDiagnosticState
    changes?: Array<RenderPreferences & { at: string }>; error?: string
  }> }
}

function useWindowDiagnostics<T>(scope: 'business' | 'rendering', command: string) {
  const snapshot = shallowRef<T | null>(null)
  const loading = ref(false)
  const error = ref('')
  let generation = 0
  let waitTimer: ReturnType<typeof setTimeout> | undefined
  let finishWaiting: (() => void) | undefined

  function invalidate() {
    generation++
    if (waitTimer !== undefined) clearTimeout(waitTimer)
    waitTimer = undefined
    finishWaiting?.()
    finishWaiting = undefined
    snapshot.value = null; loading.value = false; error.value = ''
  }
  async function refresh() {
    if (loading.value) return
    const request = ++generation
    const requestId = crypto.randomUUID()
    loading.value = true
    error.value = ''
    try {
      await invoke('begin_business_diagnostics', { requestId, scope })
      if (request !== generation) return
      // Give open views a bounded opportunity to answer; missing views remain explicit.
      await new Promise<void>(resolve => {
        finishWaiting = resolve
        waitTimer = setTimeout(() => { waitTimer = undefined; finishWaiting = undefined; resolve() }, 350)
      })
      if (request !== generation) return
      const result = await invoke<T>(command, { requestId })
      if (request === generation) snapshot.value = result
    } catch (failure) {
      if (request === generation) { snapshot.value = null; error.value = String(failure) }
    } finally { if (request === generation) loading.value = false }
  }
  return { snapshot, loading, error, refresh, invalidate }
}

export function useBusinessDiagnostics() {
  const windows = useWindowDiagnostics<BusinessDiagnosticSnapshot>('business', 'get_business_diagnostics')
  const outputs = ref<OutputDiagnosticSnapshot | null>(null)
  const outputsLoading = ref(false)
  const outputsError = ref('')
  let outputGeneration = 0
  function invalidateOutputs() { outputGeneration++; outputs.value = null; outputsLoading.value = false; outputsError.value = '' }
  async function inspectOutputs(paths: string[]) {
    if (outputsLoading.value) return
    const request = ++outputGeneration
    outputsLoading.value = true
    outputsError.value = ''
    try {
      const result = await invoke<OutputDiagnosticSnapshot>('inspect_diagnostic_outputs', { paths })
      if (request === outputGeneration) outputs.value = result
    } catch (failure) {
      if (request === outputGeneration) { outputs.value = null; outputsError.value = String(failure) }
    } finally { if (request === outputGeneration) outputsLoading.value = false }
  }
  return { ...windows, outputs, outputsLoading, outputsError, inspectOutputs, invalidateOutputs,
    invalidate() { windows.invalidate(); invalidateOutputs() } }
}

export function useRendererDiagnostics() { return useWindowDiagnostics<RendererDiagnosticSnapshot>('rendering', 'get_renderer_diagnostics') }
