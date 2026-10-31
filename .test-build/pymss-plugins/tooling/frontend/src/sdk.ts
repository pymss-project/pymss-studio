import { ref, watch, type Ref } from 'vue'
import { useSettingsStore } from './stores/settings'
import { presentation } from './presentation'

type JsonObject = Record<string, unknown>
type HostError = Error & { code?: string; data?: JsonObject }
type Progress = { id: number; message?: string; fraction?: number; operation?: string; event?: { type: string; payload: JsonObject } }
type WorkerEvent = { type: string; taskId?: string; requestId?: string; payload: JsonObject }
type Handlers = { resolve: (value: unknown) => void; reject: (error: HostError) => void; progress?: (update: Progress) => void; timer?: ReturnType<typeof setTimeout> }
const pending = new Map<number, Handlers>()
const listeners = new Set<(event: { payload: WorkerEvent }) => void>()
const tasks = new Map<string, number>()
let sequence = 0
let connected = false
let disposed = false
let settingsValue: JsonObject = {}
let saveQueue = Promise.resolve()
let resolveReady!: () => void
const ready = new Promise<void>(resolve => { resolveReady = resolve })
export const persistenceError = ref('')
export type UnlistenFn = () => void

export function isStudioHost() { return connected && !disposed }

export function call<T = unknown>(method: string, params: JsonObject = {}, progress?: (update: Progress) => void) {
  const id = ++sequence
  // Tool state can contain Vue proxies. The public bridge carries JSON values.
  const request = JSON.parse(JSON.stringify({ jsonrpc: '2.0', id, method, params }))
  const longRunning = method === 'operations.start' || method === 'audio.analyze' || method === 'runtime.managePackage' || method.startsWith('files.pick') || method === 'operations.cancel'
  const promise = new Promise<T>((resolve, reject) => {
    const timer = longRunning ? undefined : setTimeout(() => {
      pending.delete(id)
      reject(new Error('Studio did not respond'))
    }, 30000)
    pending.set(id, { resolve: value => resolve(value as T), reject, timer, progress })
    window.parent.postMessage(request, '*')
  })
  return { id, promise }
}

window.addEventListener('message', event => {
  if (disposed || event.source !== window.parent || window.parent === window || event.data?.jsonrpc !== '2.0') return
  const data = event.data
  if (data.method === 'host.ready') {
    connected = true
    resolveReady()
  } else if (data.method === 'host.overlay') {
    const root = document.documentElement
    const bounds = data.params?.bounds
    const anchored = data.params?.open === true && bounds
      && [bounds.left, bounds.top, bounds.width].every(value => typeof value === 'number' && Number.isFinite(value))
      && bounds.width > 0
    root.classList.toggle('tool-overlay-anchored', Boolean(anchored))
    for (const key of ['left', 'top', 'width']) {
      if (anchored) root.style.setProperty(`--tool-overlay-${key}`, `${bounds[key]}px`)
      else root.style.removeProperty(`--tool-overlay-${key}`)
    }
  } else if (data.method === 'host.theme') {
    for (const [key, value] of Object.entries(data.params?.cssVariables || {})) {
      if (/^--[a-z0-9-]+$/i.test(key) && typeof value === 'string') document.documentElement.style.setProperty(key, value)
    }
    if (data.params?.colorMode === 'light' || data.params?.colorMode === 'dark') { presentation.mode = data.params.colorMode; document.documentElement.dataset.mode = data.params.colorMode }
    if (typeof data.params?.fontFamily === 'string') { presentation.fontFamily = data.params.fontFamily; document.body.style.fontFamily = data.params.fontFamily }
    if (typeof data.params?.locale === 'string') presentation.locale = data.params.locale
    if (data.params?.componentTheme && typeof data.params.componentTheme === 'object' && !Array.isArray(data.params.componentTheme)) presentation.componentTheme = data.params.componentTheme
    if (typeof data.params?.cssVariables?.['--primary'] === 'string') presentation.primary = data.params.cssVariables['--primary']
  } else if (data.method === 'task.progress') {
    pending.get(data.params?.id)?.progress?.(data.params)
  } else if (pending.has(data.id)) {
    const handlers = pending.get(data.id)!
    pending.delete(data.id)
    clearTimeout(handlers.timer)
    if (data.error) {
      const error: HostError = new Error(String(data.error.message || 'Studio request failed'))
      error.data = data.error.data
      error.code = data.error.data?.code
      handlers.reject(error)
    } else handlers.resolve(data.result)
  }
})

export async function connect() {
  window.parent.postMessage({ jsonrpc: '2.0', method: 'pymss.ready' }, '*')
  await ready
  const info = await call<{ sdkVersion: number; capabilities: string[]; locale: string; defaultOutputDir?: string; defaultModelDir?: string; runtimeInfo?: { platform?: string; machine?: string } }>('app.info').promise
  if (info.sdkVersion !== __STUDIO_REQUIREMENTS__.sdkVersion) {
    throw new Error(`This plugin requires Studio SDK ${__STUDIO_REQUIREMENTS__.sdkVersion}; the host provides ${info.sdkVersion ?? 'no SDK version'}.`)
  }
  const capabilities = new Set(info.capabilities)
  const missing = __STUDIO_REQUIREMENTS__.requiredCapabilities.filter(capability => !capabilities.has(capability))
  if (missing.length) throw new Error(`Studio is missing required plugin capabilities: ${missing.join(', ')}`)
  presentation.locale = info.locale
  const settings = useSettingsStore()
  settings.outputDir = info.defaultOutputDir || ''
  settings.modelDir = info.defaultModelDir || ''
  settings.platform = info.runtimeInfo?.platform || ''
  settings.machine = info.runtimeInfo?.machine || ''
  settingsValue = await call<JsonObject>('settings.read').promise
  return info
}

export async function readSettings() {
  await saveQueue.catch(() => undefined)
  return structuredClone(settingsValue)
}

export function writeSettings(value: JsonObject) {
  const snapshot = JSON.parse(JSON.stringify(value)) as JsonObject
  settingsValue = snapshot
  const save = saveQueue.catch(() => undefined).then(() => call('settings.write', { data: snapshot }).promise).then(() => { persistenceError.value = '' })
  saveQueue = save
  void save.catch(error => { persistenceError.value = error instanceof Error ? error.message : String(error) })
  return save
}

export function toolRef<T>(key: string, initial: T, legacyKey?: string): Ref<T> {
  const form = settingsValue.form as JsonObject | undefined
  const saved = form && Object.hasOwn(form, key) ? form[key] : legacyKey ? settingsValue[legacyKey] : undefined
  const valid = Array.isArray(initial)
    ? Array.isArray(saved) && saved.every(value => typeof value === 'string')
    : initial === null ? saved === null || (typeof saved === 'number' && Number.isFinite(saved))
    : typeof initial === 'object' ? !!saved && typeof saved === 'object' && !Array.isArray(saved)
    : typeof saved === typeof initial && (typeof saved !== 'number' || Number.isFinite(saved))
  const value = ref(valid ? saved : initial) as Ref<T>
  watch(value, current => {
    void writeSettings({ ...settingsValue, form: { ...(settingsValue.form as JsonObject || {}), [key]: current } }).catch(() => undefined)
  }, { deep: true, flush: 'sync' })
  return value
}

function emit(type: string, id: string, payload: JsonObject) {
  for (const listener of listeners) listener({ payload: { type, taskId: id, requestId: id, payload } })
}

function start(method: string, id: string, params: JsonObject, resultEvent: string, operation?: string) {
  const job = call(method, params, update => {
    const event = update.event
    if (event && typeof event.type === 'string' && event.payload && typeof event.payload === 'object') emit(event.type, id, event.payload)
    else if (operation === 'convert' && typeof update.fraction === 'number' && Number.isFinite(update.fraction)) {
      emit('audio_tool_progress', id, { operation, phase: 'converting', completed: Math.round(Math.min(1, Math.max(0, update.fraction)) * 100), total: 100, current: update.message || '' })
    }
  })
  tasks.set(id, job.id)
  void job.promise.then(result => {
    const payload = method === 'operations.start'
      ? (result as { outputs: { report: { value: JsonObject } } }).outputs.report.value
      : result as JsonObject
    emit(resultEvent, id, operation === 'convert' ? { ...payload, operation } : payload)
  }).catch((error: HostError) => {
    emit(error.code === 'TASK_CANCELLED' ? 'task_cancelled' : 'error', id, {
      ...error.data, code: error.code || 'AUDIO_TOOL_FAILED', message: error.message, ...(operation ? { operation } : {}),
    })
  }).finally(() => tasks.delete(id))
  return { taskId: id, started: true }
}

export async function invoke<T = unknown>(command: string, args: JsonObject = {}): Promise<T> {
  const payload = args.payload as JsonObject || {}
  let value: unknown
  switch (command) {
    case 'pick_single_audio_file': value = (await call<{ path: string } | null>('files.pickAudio').promise)?.path || null; break
    case 'pick_audio_files': value = (await call<{ paths: string[] }>('files.pickAudio', { multiple: true }).promise).paths; break
    case 'pick_input_folder':
    case 'pick_output_folder': value = (await call<{ path: string } | null>('files.pickDirectory').promise)?.path || null; break
    case 'pick_model_weights_file': value = (await call<{ path: string } | null>('files.pickModel', { title: args.title }).promise)?.path || null; break
    case 'scan_audio_paths_with_options': value = await call('files.scanAudio', { paths: args.paths, recursive: args.recursive }).promise; break
    case 'reveal_path': value = await call('files.reveal', { path: args.path }).promise; break
    case 'start_audio_tool': {
      if (payload.operation !== __FILE_TOOL__ || !__OPERATION_ID__) throw new Error('Unsupported plugin operation')
      const conversion = payload.operation === 'convert'
      value = start('operations.start', String(payload.requestId), {
        operation: __OPERATION_ID__,
        inputs: conversion ? { files: { paths: payload.inputs } } : { request: payload },
        params: conversion ? {
          output_dir: payload.outputDir, format: payload.outputFormat, sample_rate: payload.sampleRate, channels: payload.channels,
          wav_bit_depth: payload.wavBitDepth, flac_bit_depth: payload.flacBitDepth, mp3_bit_rate: payload.mp3BitRate, ogg_bit_rate: payload.oggBitRate,
        } : {},
        ...(typeof payload.outputDir === 'string' && payload.outputDir.trim() ? { outputDir: payload.outputDir } : {}),
      }, 'audio_tool_result', __FILE_TOOL__)
      break
    }
    case 'start_tempo_key_analysis':
      value = start('audio.analyze', String(payload.requestId), { payload }, 'audio_tool_result', String(payload.operation)); break
    case 'cancel_task': {
      const id = tasks.get(String(args.taskId))
      value = id === undefined ? false : await call<boolean>('operations.cancel', { id }).promise
      break
    }
    case 'optional_runtime_package_status': value = await call('runtime.packageStatus', { package: args.package || payload.package }).promise; break
    case 'manage_optional_runtime_package': value = start('runtime.managePackage', String(payload.taskId), { payload }, 'optional_package_status'); break
    case 'delete_tool_model': value = await call('models.deleteTool', { payload }).promise; break
    default: throw new Error(`Unsupported Studio tool service: ${command}`)
  }
  return value as T
}

export async function listen<T>(_name: string, callback: (event: { payload: T }) => void): Promise<UnlistenFn> {
  if (_name !== 'pymss://worker-event') throw new Error('Unsupported tool event channel')
  const handler = callback as (event: { payload: WorkerEvent }) => void
  listeners.add(handler)
  return () => { listeners.delete(handler) }
}

export function open(path: string) { return call('shell.openExternal', { url: path }).promise }
export function writeClipboardText(text: string) { return call('clipboard.writeText', { text }).promise }

window.addEventListener('pagehide', () => {
  disposed = true
  for (const handlers of pending.values()) {
    clearTimeout(handlers.timer)
    handlers.reject(new Error('Tool page closed'))
  }
  pending.clear()
  listeners.clear()
}, { once: true })
