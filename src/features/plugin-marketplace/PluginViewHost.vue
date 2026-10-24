<script setup lang="ts">
/**
 * Plugin view host: loads a plugin-contributed page (protocol v1 view
 * contribution) inside a sandboxed iframe and bridges the pymss Studio SDK
 * (JSON-RPC over postMessage) to Tauri commands.
 *
 * Security posture: the iframe is sandboxed (no same-origin), only manifest
 * allowlisted SDK methods pass, and every incoming message is checked against
 * "source must be this iframe".
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { open } from '@tauri-apps/plugin-dialog'

type PluginViewContribution = {
  id: string
  title: string
  entry: string
  entryPath?: string
  placements: string[]
  operation?: string | null
  permissions: string[]
}

const props = defineProps<{ pluginName: string; view: PluginViewContribution }>()
const { t } = useI18n()

const frame = ref<HTMLIFrameElement | null>(null)
const status = ref<'loading' | 'ready' | 'error'>('loading')
const errorText = ref('')
const src = ref('')

const permissions = () => props.view.permissions || ['operations']

function allowed(method: string): boolean {
  const family = method.split('.')[0]
  const list = permissions()
  return list.includes(method) || list.includes(family)
}

type RunHandlers = {
  resolve: (value: unknown) => void
  reject: (error: Error) => void
  onProgress?: (payload: Record<string, unknown>) => void
}
const runRequests = new Map<string, RunHandlers>()

async function handleCall(id: number, method: string, params: Record<string, unknown>) {
  if (!allowed(method)) throw new Error(`PERMISSION_DENIED: ${method}`)
  switch (method) {
    case 'app.info': {
      const build = await invoke<{ version: string }>('get_build_info')
      return { studio: 'pymss-studio', version: build.version, locale: t('plugins.sdkLocale' as never) || 'en' }
    }
    case 'operations.describe':
      return await invoke('describe_operation', { payload: { operation: params.operation } })
    case 'operations.start': {
      const requestId = `plugin_view_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
      const promise = new Promise<unknown>((resolve, reject) => {
        runRequests.set(requestId, { resolve, reject })
      })
      await invoke('start_operation_task', {
        payload: {
          operation: params.operation,
          inputs: params.inputs || {},
          params: params.params || {},
          requestId,
        },
      })
      return await promise
    }
    case 'files.pickAudio': {
      const selected = await open({ multiple: false, title: 'Audio' })
      return typeof selected === 'string' ? { path: selected } : null
    }
    default:
      throw new Error(`UNKNOWN_METHOD: ${method}`)
  }
}

function postToFrame(message: Record<string, unknown>) {
  frame.value?.contentWindow?.postMessage(message, '*')
}

const THEME_TOKENS = [
  '--surface', '--surface-1', '--surface-2', '--surface-3',
  '--on-surface', '--on-surface-heading', '--on-surface-muted',
  '--outline', '--outline-strong',
  '--primary', '--primary-strong', '--primary-soft',
  '--danger', '--success', '--warning', '--radius', '--radius-small',
]

/** Capture Studio's live design tokens and push them into the plugin iframe. */
function pushTheme() {
  const rootStyle = getComputedStyle(document.documentElement)
  const cssVariables: Record<string, string> = {}
  for (const token of THEME_TOKENS) {
    const value = rootStyle.getPropertyValue(token).trim()
    if (value) cssVariables[token] = value
  }
  postToFrame({
    jsonrpc: '2.0',
    method: 'host.theme',
    params: {
      cssVariables,
      colorMode: document.documentElement.classList.contains('light-theme') ? 'light' : 'dark',
      fontFamily: getComputedStyle(document.body).fontFamily,
    },
  })
}

function onWindowMessage(event: MessageEvent) {
  const frameWindow = frame.value?.contentWindow
  if (!frameWindow || event.source !== frameWindow) return
  const data = event.data
  if (!data || typeof data !== 'object' || data.jsonrpc !== '2.0') return
  // Page signals readiness; the host may pre-send cached state in response.
  if (typeof data.method === 'string' && data.method === 'pymss.ready') {
    postToFrame({ jsonrpc: '2.0', method: 'host.ready' })
    pushTheme()
    return
  }
  const id = Number(data.id)
  if (!Number.isFinite(id) || typeof data.method !== 'string') return
  handleCall(id, data.method, data.params || {})
    .then(result => postToFrame({ jsonrpc: '2.0', id, result }))
    .catch(failure => postToFrame({
      jsonrpc: '2.0',
      id,
      error: { code: -32000, message: failure instanceof Error ? failure.message : String(failure) },
    }))
}

let unlistenWorker: UnlistenFn | undefined
let themeObserver: MutationObserver | undefined

onMounted(async () => {
  window.addEventListener('message', onWindowMessage)
  // theme.ts flips .dark-theme/.light-theme on <html> and rewrites token
  // properties via element style; observe class + inline style so plugin
  // pages follow light/dark and accent changes without reloading.
  themeObserver = new MutationObserver(() => pushTheme())
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'style'] })
  try {
    if (!props.view.entryPath && !props.view.entry) throw new Error('view entry not found')
    src.value = `pymss-plugin://view/${encodeURIComponent(props.pluginName)}/${encodeURIComponent(props.view.id)}/index.html`
    status.value = 'loading'
  } catch (failure) {
    status.value = 'error'
    errorText.value = String(failure)
  }
  unlistenWorker = await listen<{ type: string; requestId?: string; taskId?: string; payload: Record<string, unknown> }>(
    'pymss://worker-event',
    ({ payload: workerEvent }) => {
      const requestId = workerEvent.requestId || workerEvent.taskId || ''
      const handlers = runRequests.get(requestId)
      if (!handlers) return
      if (workerEvent.type === 'operation_result') {
        runRequests.delete(requestId)
        handlers.resolve(workerEvent.payload)
      } else if (workerEvent.type === 'error' || workerEvent.type === 'task_cancelled') {
        runRequests.delete(requestId)
        handlers.reject(new Error(String(workerEvent.payload?.message || workerEvent.type)))
      } else if (workerEvent.type === 'operation_progress') {
        handlers.onProgress?.(workerEvent.payload || {})
        postToFrame({ jsonrpc: '2.0', method: 'task.progress', params: { requestId, ...(workerEvent.payload || {}) } })
      }
    },
  )
})

onBeforeUnmount(() => {
  window.removeEventListener('message', onWindowMessage)
  unlistenWorker?.()
  themeObserver?.disconnect()
})
</script>

<template>
  <div class="plugin-view-host">
    <n-alert v-if="status === 'error'" type="error">{{ errorText }}</n-alert>
    <iframe
      v-show="status !== 'error'"
      ref="frame"
      class="plugin-view-frame"
      :src="src"
      sandbox="allow-scripts allow-forms"
      :title="view.title || pluginName"
      @load="pushTheme"
    />
  </div>
</template>

<style scoped>
.plugin-view-host { display: flex; flex-direction: column; height: 100%; min-height: 320px; }
.plugin-view-frame { flex: 1; width: 100%; border: 0; background: transparent; }
</style>
