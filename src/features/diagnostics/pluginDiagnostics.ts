import { ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'

export type PluginDiagnosticSnapshot = {
  checkedAt: string
  runtime: {
    pythonPath: string
    configuredPythonPath: string | null
    interpreterMatches: boolean | null
    bindingDir: string
    bindingError?: string | null
    identity: string
    pymssVersion: string | null
    managementSupported: boolean
    pluginApisSupported: boolean
    apiError: string | null
  }
  plugins: Array<{
    name: string
    version: string | null
    enabled: boolean
    path: string
    loaded: boolean | null
    error: string | null
  }>
  verification: { performed: boolean; error: string | null }
}

export function usePluginDiagnostics() {
  const snapshot = ref<PluginDiagnosticSnapshot | null>(null)
  const loading = ref(false)
  const verifying = ref(false)
  const error = ref('')
  let generation = 0
  let pending: Promise<void> | undefined

  function invalidate() {
    generation++
    snapshot.value = null
    error.value = ''
    loading.value = false
    verifying.value = false
    pending = undefined
  }

  function refresh(verify = false): Promise<void> {
    if (pending) return pending
    const request = ++generation
    loading.value = true
    verifying.value = verify
    error.value = ''
    pending = (async () => {
      try {
        const result = await invoke<PluginDiagnosticSnapshot>('plugin_diagnostics', { payload: { verify } })
        if (request === generation) snapshot.value = result
      } catch (failure) {
        if (request === generation) {
          snapshot.value = null
          error.value = String(failure)
        }
      } finally {
        if (request === generation) {
          loading.value = false
          verifying.value = false
          pending = undefined
        }
      }
    })()
    return pending
  }

  return { snapshot, loading, verifying, error, refresh, invalidate }
}
