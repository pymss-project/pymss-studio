import { computed, ref, shallowRef, watch } from 'vue'
import { defineStore } from 'pinia'
import { invoke } from '@tauri-apps/api/core'
import { pluginRevision } from '@/features/plugin-marketplace/composable'
import { selectPluginNodeDeclarations, type PluginNodeDecl } from '@/litegraph/nodeSpecs'
import { isTauriRuntime } from '@/utils/appStore'

/** Live node capabilities belong to the active runtime, never the saved graph. */
export const useWorkflowNodeStore = defineStore('workflowNodes', () => {
  const declarations = shallowRef<PluginNodeDecl[]>([])
  const loading = ref(false)
  const error = ref('')
  let loadedRevision = -1
  let pending: { revision: number; promise: Promise<PluginNodeDecl[]> } | undefined
  const saveNodeTypes = computed(() => new Set(declarations.value
    .filter(node => node.type && node.registered === true && node.savesOutput === true)
    .map(node => node.type!)))

  function load(): Promise<PluginNodeDecl[]> {
    if (!isTauriRuntime()) return Promise.resolve(declarations.value)
    const revision = pluginRevision.value
    if (loadedRevision === revision) return Promise.resolve(declarations.value)
    if (pending?.revision === revision) return pending.promise
    loading.value = true
    error.value = ''
    const promise = invoke<{ nodes?: PluginNodeDecl[] }>('list_nodes')
      .then(response => {
        if (revision !== pluginRevision.value) return load()
        declarations.value = selectPluginNodeDeclarations(Array.isArray(response?.nodes) ? response.nodes : [])
        loadedRevision = revision
        return declarations.value
      }, failure => {
        if (revision !== pluginRevision.value) return load()
        error.value = failure instanceof Error ? failure.message : String(failure)
        throw failure
      })
      .finally(() => {
        if (pending?.promise === promise) {
          pending = undefined
          loading.value = false
        }
      })
    pending = { revision, promise }
    return promise
  }

  watch(pluginRevision, () => {
    declarations.value = []
    loadedRevision = -1
    void load().catch(() => undefined)
  }, { flush: 'sync' })

  return { declarations, saveNodeTypes, loading, error, load }
})
