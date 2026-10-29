<script setup lang="ts">
/**
 * Plugin view host: loads a plugin-contributed page inside a sandboxed iframe
 * and bridges the pymss Studio SDK
 * (JSON-RPC over postMessage) to Tauri commands.
 *
 * Security posture: the iframe is sandboxed (no same-origin), only manifest
 * allowlisted SDK methods pass, and every incoming message is checked against
 * "source must be this iframe".
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { convertFileSrc, invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { open } from '@tauri-apps/plugin-dialog'
import { open as openExternal } from '@tauri-apps/plugin-shell'
import { pluginViewUrl } from './viewUrl'
import { useSettingsStore } from '@/stores/settings'
import { useAppStore } from '@/stores/app'
import { registerWindowCloseGuard } from '@/utils/windowCloseGuards'
import { getThemeOverrides } from '@/utils/theme'
import { pluginSdkCapabilities, pluginSdkVersion, supportsPluginCapability } from './sdkContract'

type PluginViewContribution = {
  id: string
  title: string
  entry: string
  entryPath?: string
  placements: string[]
  operation?: string | null
  permissions: string[]
  sdkVersion?: number
  requiredCapabilities?: string[]
}

const props = defineProps<{ pluginName: string; pluginsRoot: string; view: PluginViewContribution }>()
const emit = defineEmits<{ 'busy-change': [busy: boolean] }>()
const { t, locale } = useI18n()
const settings = useSettingsStore()
const app = useAppStore()

const frame = ref<HTMLIFrameElement | null>(null)
const status = ref<'loading' | 'ready' | 'error'>('loading')
const errorText = ref('')
const src = ref('')
const measuredHeight = ref(640)
const overlayOpen = ref(false)
let disposed = false

const MIN_VIEW_HEIGHT = 120
const MAX_VIEW_HEIGHT = 20_000

const permissions = () => props.view.permissions
const sdkInfo = () => ({ sdkVersion: pluginSdkVersion, capabilities: pluginSdkCapabilities })

function allowed(method: string): boolean {
  if (!supportsPluginCapability(method)) return false
  const family = method.split('.')[0]
  const list = permissions()
  return list.includes(method) || list.includes(family)
}

type RunHandlers = {
  rpcId: number
  resolve: (value: unknown) => void
  reject: (error: Error) => void
  nativeTaskId?: string
  cancelRequested: boolean
  startAcknowledged: Promise<boolean>
  resolveStartAcknowledged: (started: boolean) => void
  cancelPromise?: Promise<boolean>
  settled: Promise<void>
  resolveSettled: () => void
  kind: 'operation' | 'audio' | 'package'
}
const runRequests = new Map<string, RunHandlers>()

class TaskCancelledError extends Error {
  readonly code = 'TASK_CANCELLED'
  constructor(message = 'Task cancelled') {
    super(message)
    this.name = 'TaskCancelledError'
  }
}

class WorkerRequestError extends Error {
  readonly data: Record<string, unknown>
  readonly code?: string
  constructor(message: string, data: Record<string, unknown>) {
    super(message)
    this.name = 'WorkerRequestError'
    this.data = data
    this.code = typeof data.code === 'string' ? data.code : undefined
  }
}

function syncBusy() {
  emit('busy-change', runRequests.size > 0)
}

function plainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function boundedPath(value: unknown, label: string): string {
  if (typeof value !== 'string' || !value.trim() || value.length > 4096) throw new Error(`INVALID_${label.toUpperCase()}`)
  return value.trim()
}

function boundedPaths(value: unknown): string[] {
  if (!Array.isArray(value) || !value.length || value.length > 256) throw new Error('INVALID_PATHS')
  return value.map(path => boundedPath(path, 'path'))
}

function workerError(payload: Record<string, unknown>) {
  return new WorkerRequestError(String(payload.message || 'Worker operation failed'), payload)
}

function isAllowedExternalUrl(value: unknown) {
  try {
    const url = new URL(String(value || ''))
    const host = url.hostname.toLowerCase()
    return url.protocol === 'https:' && ['github.com', 'huggingface.co', 'modelscope.cn']
      .some(domain => host === domain || host.endsWith(`.${domain}`))
  } catch { return false }
}

async function cancelOwned(requestId: string, handlers: RunHandlers) {
  handlers.cancelRequested = true
  if (handlers.cancelPromise) return await handlers.cancelPromise
  const cancellation = (async () => {
    if (!handlers.nativeTaskId && !await handlers.startAcknowledged) return false
    const taskId = handlers.nativeTaskId
    return taskId ? await invoke<boolean>('cancel_task', { taskId }) : false
  })()
  handlers.cancelPromise = cancellation
  try {
    return await cancellation
  } catch (failure) {
    if (handlers.cancelPromise === cancellation) handlers.cancelPromise = undefined
    throw failure
  }
}

function settleRun(requestId: string, failure?: Error, value?: unknown) {
  const handlers = runRequests.get(requestId)
  if (!handlers) return
  runRequests.delete(requestId)
  syncBusy()
  if (failure) handlers.reject(failure)
  else handlers.resolve(value)
  handlers.resolveSettled()
}

async function startBackground(
  rpcId: number,
  command: 'start_operation_task' | 'start_tempo_key_analysis' | 'manage_optional_runtime_package',
  kind: RunHandlers['kind'],
  payload: Record<string, unknown>,
) {
  const requestId = `plugin_view_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
  let resolveStartAcknowledged!: (started: boolean) => void
  const startAcknowledged = new Promise<boolean>((resolve) => { resolveStartAcknowledged = resolve })
  let resolveSettled!: () => void
  const settled = new Promise<void>((resolve) => { resolveSettled = resolve })
  const completion = new Promise<unknown>((resolve, reject) => {
    runRequests.set(requestId, {
      rpcId, resolve, reject, cancelRequested: false, startAcknowledged, resolveStartAcknowledged,
      settled, resolveSettled, kind,
    })
  })
  const handlers = runRequests.get(requestId)!
  syncBusy()
  void completion.catch(() => undefined)
  try {
    const started = await invoke<{ taskId?: string }>(command, {
      payload: { ...payload, requestId, taskId: requestId },
    })
    handlers.nativeTaskId = started?.taskId || requestId
    handlers.resolveStartAcknowledged(true)
    if (handlers.cancelRequested) void cancelOwned(requestId, handlers).catch(() => undefined)
    return await completion
  } catch (failure) {
    handlers.resolveStartAcknowledged(false)
    settleRun(requestId, failure instanceof Error ? failure : new Error(String(failure)))
    throw failure
  } finally {
    if (!runRequests.has(requestId)) syncBusy()
  }
}

async function closeRunningOperations() {
  const pending = [...runRequests.entries()]
  for (const [requestId, handlers] of pending) {
    try {
      const accepted = await cancelOwned(requestId, handlers)
      if (!accepted) settleRun(requestId, new Error('Operation task is no longer running'))
      await handlers.settled
    } catch (failure) {
      // Keep the request and its listener alive so the user can retry cancellation.
      throw failure
    }
  }
}

const unregisterCloseGuard = registerWindowCloseGuard(closeRunningOperations, 80)

async function handleCall(id: number, method: string, params: Record<string, unknown>) {
  if (!supportsPluginCapability(method)) throw new Error(`UNSUPPORTED_CAPABILITY: ${method}`)
  if (!allowed(method)) throw new Error(`PERMISSION_DENIED: ${method}`)
  switch (method) {
    case 'app.info': {
      const build = await invoke<{ version: string }>('get_build_info')
      const runtime = app.runtimeInfo
      return {
        studio: 'pymss-studio', version: build.version, locale: String(locale.value),
        ...sdkInfo(),
        defaultOutputDir: settings.outputDir, defaultModelDir: settings.modelDir,
        runtimeInfo: runtime ? {
          platform: runtime.platform,
          machine: runtime.machine,
          backend: runtime.torchBackend,
          packages: runtime.packageVersions || runtime.packages,
        } : null,
      }
    }
    case 'operations.describe':
      return await invoke('describe_operation', { payload: { operation: params.operation } })
    case 'operations.start': {
      return await startBackground(id, 'start_operation_task', 'operation', {
        operation: params.operation, inputs: params.inputs || {}, params: params.params || {},
        ...(typeof params.outputDir === 'string' && params.outputDir.trim() ? { outputDir: params.outputDir.trim() } : {}),
      })
    }
    case 'operations.cancel': {
      const rpcId = Number(params.id)
      if (!Number.isSafeInteger(rpcId)) throw new Error('INVALID_OPERATION_ID')
      const entry = [...runRequests.entries()].find(([, handlers]) => handlers.rpcId === rpcId)
      if (!entry) throw new Error('OPERATION_NOT_OWNED')
      return await cancelOwned(entry[0], entry[1])
    }
    case 'files.pickAudio': {
      const multiple = params.multiple === true
      const selected = await open({ multiple, title: 'Audio' })
      if (multiple) return Array.isArray(selected) ? { paths: selected } : { paths: [] }
      return typeof selected === 'string' ? { path: selected } : null
    }
    case 'files.pickDirectory': {
      const selected = await open({ directory: true, multiple: false, title: 'Directory' })
      return typeof selected === 'string' ? { path: selected } : null
    }
    case 'files.pickModel': {
      const title = typeof params.title === 'string' && params.title.length <= 200 ? params.title : undefined
      const selected = await invoke<string | null>('pick_model_weights_file', title ? { title } : {})
      return typeof selected === 'string' ? { path: selected } : null
    }
    case 'files.scanAudio':
      return await invoke('scan_audio_paths_with_options', {
        paths: boundedPaths(params.paths), recursive: params.recursive !== false, sortFiles: true,
      })
    case 'files.reveal':
      await invoke('reveal_path', { path: boundedPath(params.path, 'path') })
      return { ok: true }
    case 'clipboard.writeText': {
      if (typeof params.text !== 'string' || params.text.length > 1024 * 1024) throw new Error('INVALID_CLIPBOARD_TEXT')
      await navigator.clipboard.writeText(params.text)
      return { ok: true }
    }
    case 'shell.openExternal':
      if (!isAllowedExternalUrl(params.url)) throw new Error('EXTERNAL_URL_NOT_ALLOWED')
      await openExternal(String(params.url))
      return { ok: true }
    case 'settings.read': {
      const value = await invoke<unknown>('load_plugin_tool_settings', {
        pluginName: props.pluginName, viewId: props.view.id,
      })
      return plainObject(value) ? value : {}
    }
    case 'settings.write': {
      if (!plainObject(params.data)) throw new Error('INVALID_SETTINGS')
      let value: unknown
      try {
        value = JSON.parse(JSON.stringify(params.data))
      } catch {
        throw new Error('INVALID_SETTINGS')
      }
      if (!plainObject(value)) throw new Error('INVALID_SETTINGS')
      await invoke('save_plugin_tool_settings', {
        pluginName: props.pluginName, viewId: props.view.id, data: value,
      })
      return value
    }
    case 'audio.analyze': {
      const payload = params.payload
      if (!plainObject(payload) || (payload.operation !== 'tempo_key' && !(payload.operation === 'midi' && payload.action === 'detect_bpm'))) {
        throw new Error('AUDIO_ANALYSIS_NOT_ALLOWED')
      }
      return await startBackground(id, 'start_tempo_key_analysis', 'audio', payload)
    }
    case 'runtime.packageStatus': {
      if (params.package !== 'funasr') throw new Error('PACKAGE_NOT_ALLOWED')
      return await invoke('optional_runtime_package_status', { package: 'funasr' })
    }
    case 'runtime.managePackage': {
      const payload = params.payload
      if (!plainObject(payload) || payload.package !== 'funasr' || !['install', 'uninstall'].includes(String(payload.action))) {
        throw new Error('PACKAGE_NOT_ALLOWED')
      }
      return await startBackground(id, 'manage_optional_runtime_package', 'package', payload)
    }
    case 'models.deleteTool': {
      const payload = params.payload
      if (!plainObject(payload) || payload.tool !== 'asr') throw new Error('TOOL_MODEL_NOT_ALLOWED')
      return await invoke('delete_tool_model', { payload })
    }
    default:
      throw new Error(`UNKNOWN_METHOD: ${method}`)
  }
}

function postToFrame(message: Record<string, unknown>) {
  if (disposed) return
  const target = frame.value?.contentWindow
  if (!target) return
  // Store values can contain Vue proxies; the public bridge transports JSON.
  target.postMessage(JSON.parse(JSON.stringify(message)), '*')
}

const THEME_TOKENS = [
  '--surface', '--surface-1', '--surface-2', '--surface-3',
  '--on-surface', '--on-surface-heading', '--on-surface-muted',
  '--outline', '--outline-strong',
  '--primary', '--primary-strong', '--primary-soft', '--primary-border',
  '--danger', '--success', '--warning', '--radius', '--radius-small',
  '--shadow-panel', '--font-mono',
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
      locale: locale.value,
      componentTheme: getThemeOverrides(settings.themeMode, settings.themeAccent),
    },
  })
}

watch(locale, pushTheme)

function onWindowMessage(event: MessageEvent) {
  if (disposed) return
  const frameWindow = frame.value?.contentWindow
  if (!frameWindow || event.source !== frameWindow) return
  const data = event.data
  if (!data || typeof data !== 'object' || data.jsonrpc !== '2.0') return
  if (data.method === 'pymss.overlay') {
    if (allowed('ui.overlay') && typeof data.params?.open === 'boolean') overlayOpen.value = data.params.open
    return
  }
  if (data.method === 'pymss.resize') {
    const height = data.params?.height
    if (!overlayOpen.value && typeof height === 'number' && Number.isFinite(height) && height >= MIN_VIEW_HEIGHT && height <= MAX_VIEW_HEIGHT) {
      measuredHeight.value = Math.round(height)
    }
    return
  }
  // Page signals readiness; the host may pre-send cached state in response.
  if (typeof data.method === 'string' && data.method === 'pymss.ready') {
    status.value = 'ready'
    postToFrame({ jsonrpc: '2.0', method: 'host.ready', params: sdkInfo() })
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
      error: {
        code: -32000,
        message: failure instanceof Error ? failure.message : String(failure),
        ...(failure instanceof TaskCancelledError ? { data: { code: failure.code } }
          : failure instanceof WorkerRequestError ? { data: failure.data } : {}),
      },
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
    if (props.view.sdkVersion !== pluginSdkVersion) {
      throw new Error(t('plugins.sdkVersionUnsupported', { required: props.view.sdkVersion ?? t('common.notSet'), current: pluginSdkVersion }))
    }
    const missingCapabilities = (props.view.requiredCapabilities || []).filter(capability => !supportsPluginCapability(capability))
    if (missingCapabilities.length) {
      throw new Error(t('plugins.sdkCapabilitiesUnavailable', { capabilities: missingCapabilities.join(', ') }))
    }
    if (!props.view.entryPath && !props.view.entry) throw new Error('view entry not found')
    if (!props.pluginsRoot) throw new Error('plugins root not ready')
    await invoke('set_plugin_view_root', { root: props.pluginsRoot })
    if (disposed) return
    const unlisten = await listen<{ type: string; requestId?: string; taskId?: string; payload: Record<string, unknown> }>(
      'pymss://worker-event',
      ({ payload: workerEvent }) => {
        const requestId = workerEvent.requestId || workerEvent.taskId || ''
        const handlers = runRequests.get(requestId)
        if (!handlers) return
        if (handlers.kind === 'operation' && workerEvent.type === 'operation_result') {
          settleRun(requestId, undefined, workerEvent.payload)
        } else if (handlers.kind === 'audio' && workerEvent.type === 'audio_tool_result') {
          settleRun(requestId, undefined, workerEvent.payload)
        } else if (handlers.kind === 'package' && workerEvent.type === 'optional_package_status') {
          settleRun(requestId, undefined, workerEvent.payload)
        } else if (workerEvent.type === 'error' || workerEvent.type === 'task_cancelled') {
          const message = String(workerEvent.payload?.message || workerEvent.type)
          settleRun(requestId, workerEvent.type === 'task_cancelled'
            ? new TaskCancelledError(message)
            : workerError(workerEvent.payload || {}))
        } else if (workerEvent.type === 'operation_progress') {
          postToFrame({ jsonrpc: '2.0', method: 'task.progress', params: { ...(workerEvent.payload || {}), requestId, id: handlers.rpcId } })
        } else if (handlers.kind === 'audio' && workerEvent.type === 'audio_tool_progress') {
          postToFrame({ jsonrpc: '2.0', method: 'task.progress', params: {
            requestId, id: handlers.rpcId, event: { type: workerEvent.type, payload: workerEvent.payload },
          } })
        } else if (handlers.kind === 'package' && ['optional_package_stage', 'optional_package_log'].includes(workerEvent.type)) {
          postToFrame({ jsonrpc: '2.0', method: 'task.progress', params: {
            requestId, id: handlers.rpcId, event: { type: workerEvent.type, payload: workerEvent.payload },
          } })
        }
      },
    )
    if (disposed) { unlisten(); return }
    unlistenWorker = unlisten
    src.value = pluginViewUrl(
      props.pluginName, props.view.id, props.view.entryPath || props.view.entry, convertFileSrc,
    )
    status.value = 'loading'
  } catch (failure) {
    if (disposed) return
    status.value = 'error'
    errorText.value = String(failure)
  }
})

onBeforeUnmount(() => {
  unregisterCloseGuard()
  disposed = true
  window.removeEventListener('message', onWindowMessage)
  unlistenWorker?.()
  themeObserver?.disconnect()
  for (const [requestId, handlers] of runRequests) {
    void cancelOwned(requestId, handlers).catch(() => undefined)
    settleRun(requestId, new Error('PLUGIN_VIEW_CLOSED'))
  }
})
</script>

<template>
  <div class="plugin-view-host" :style="overlayOpen ? { minHeight: `${measuredHeight}px` } : undefined">
    <n-alert v-if="status === 'error'" type="error">{{ errorText }}</n-alert>
    <iframe
      v-if="src && status !== 'error'"
      ref="frame"
      class="plugin-view-frame"
      :class="{ 'plugin-view-frame--overlay': overlayOpen }"
      :style="{ height: overlayOpen ? '100dvh' : `${measuredHeight}px` }"
      :src="src"
      sandbox="allow-scripts allow-forms"
      :title="view.title || pluginName"
      @load="pushTheme"
    />
  </div>
</template>

<style scoped>
.plugin-view-host { min-width: 0; }
.plugin-view-frame--overlay { position: fixed; inset: 0; z-index: 3000; width: 100vw !important; }
.plugin-view-frame { display: block; width: 100%; border: 0; background: transparent; }
</style>
