import { ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'

export type DiagnosticDirectory = { id: 'data' | 'models' | 'outputs' | 'settings' | 'editor' | 'logs' | 'temp'; path: string }
export type StorageDiagnosticSnapshot = { checkedAt: string; writeProbe: boolean; directories: Array<DiagnosticDirectory & {
  state: 'directory' | 'missing' | 'not_directory' | 'unreadable' | 'unresolved'
  availableBytes: number | null; readonlyAttribute?: boolean
  writeStatus: 'not_checked' | 'ok' | 'failed' | 'cleanup_failed'
  error?: string; writeError?: string; cleanupError?: string; probePath?: string
}> }

export function useStorageDiagnostics() {
  const snapshot = ref<StorageDiagnosticSnapshot | null>(null)
  const busy = ref(false)
  const error = ref('')
  let generation = 0
  function invalidate() { generation++; snapshot.value = null; error.value = '' }
  async function inspect(directories: DiagnosticDirectory[], writeProbe = false) {
    if (busy.value) return
    const request = ++generation
    busy.value = true
    error.value = ''
    try {
      const result = await invoke<StorageDiagnosticSnapshot>('inspect_diagnostic_storage', { directories, writeProbe })
      if (request === generation) snapshot.value = result
    } catch (failure) {
      if (request === generation) { snapshot.value = null; error.value = String(failure) }
    } finally { busy.value = false }
  }
  return { snapshot, busy, error, invalidate, inspect }
}
