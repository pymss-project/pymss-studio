import { computed, getCurrentScope, onScopeDispose, ref, shallowRef, watch } from 'vue'
import { defineStore } from 'pinia'
import { invoke } from '@tauri-apps/api/core'
import { pluginRevision, usePluginMarketplace } from '@/features/plugin-marketplace/composable'
import { selectPluginNodeDeclarations, type PluginNodeDecl } from '@/litegraph/nodeSpecs'
import { isTauriRuntime } from '@/utils/appStore'

/** Live node capabilities belong to the active runtime, never the saved graph. */
export const useWorkflowNodeStore = defineStore('workflowNodes', () => {
  const storeScope = getCurrentScope()!
  const market = usePluginMarketplace()
  const declarations = shallowRef<PluginNodeDecl[]>([])
  const loading = ref(false)
  const error = ref('')
  let loadedRevision = -1
  let disposed = false
  const idleWaits = new Set<() => void>()
  let pending: { revision: number; promise: Promise<PluginNodeDecl[]> } | undefined
  const saveNodeTypes = computed(() => new Set(declarations.value
    .filter(node => node.type && node.registered === true && node.savesOutput === true)
    .map(node => node.type!)))

  function load(): Promise<PluginNodeDecl[]> {
    if (disposed) return Promise.reject(new Error('Workflow node metadata store is closed'))
    if (!isTauriRuntime()) return Promise.resolve(declarations.value)
    const revision = pluginRevision.value
    if (loadedRevision === revision) return Promise.resolve(declarations.value)
    if (pending?.revision === revision) return pending.promise
    loading.value = true
    error.value = ''
    const query = () => invoke<{ nodes?: PluginNodeDecl[] }>('list_nodes')
    const response = market.busy.value || market.loading.value
      ? waitForInventory().then(() => revision === pluginRevision.value ? query() : { nodes: [] })
      : query()
    const promise = response
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
  }, { flush: 'sync' })

  // Inventory discovery may immediately hand off to default-tool preparation.
  // Let its writer reserve the runtime before metadata readers start.
  watch([pluginRevision, market.busy, market.loading], () => {
    if (!market.busy.value && !market.loading.value && loadedRevision !== pluginRevision.value) {
      void load().catch(() => undefined)
    }
  }, { flush: 'post' })

  function waitForInventory(): Promise<void> {
    return new Promise((resolve, reject) => {
      const cancel = () => { stop(); idleWaits.delete(cancel); reject(new Error('Workflow node metadata store is closed')) }
      // Shared waits belong to the store even when load() is called from a component's setup.
      const stop = storeScope.run(() => watch([market.busy, market.loading], ([busy, loading]) => {
        if (!busy && !loading) { stop(); idleWaits.delete(cancel); resolve() }
      }, { flush: 'post' }))!
      idleWaits.add(cancel)
    })
  }

  onScopeDispose(() => {
    disposed = true
    for (const cancel of idleWaits) cancel()
  })

  return { declarations, saveNodeTypes, loading, error, load }
})
