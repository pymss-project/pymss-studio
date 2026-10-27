/**
 * Web runtime bridge.
 *
 * When the studio UI runs in a plain browser (web mode), the Tauri IPC surface is
 * emulated over HTTP against the pymss-studio web server (`scripts/web-server.mjs`):
 *
 *   invoke(cmd, args)      -> POST /api/invoke/{cmd}
 *   transformCallback      -> callback registry; the server pushes events over SSE
 *                             (/api/events) and dispatches them to registered ids
 *   convertFileSrc(path)   -> /api/asset?path=... (server-side file streaming)
 *   plugin:event|listen    -> SSE-backed listener; unlisten revokes it
 *   plugin:dialog|open     -> native browser file/folder picker; uploads go to
 *                             POST /api/uploads and return server-side paths
 *
 * The module installs `window.__TAURI_INTERNALS__` so the unmodified
 * `@tauri-apps/api` modules work as-is. It must be imported before any store.
 */
import type { WebAppPaths, WebSessionInfo } from './webTypes'


type CbEntry = { handler?: (value: any) => void; once: boolean }
type EventEntry = { event: string; handler: number; target?: string }
type EventCallback<T> = (payload: { id: number; event: string; payload: T }) => void

export interface WebDialogSelection {
  paths?: string[]
  uploaded?: number
  cancelled?: boolean
}

export const WEB_RUNTIME_FLAG = '__PYMSS_WEB_RUNTIME__'

export function isWebRuntime(): boolean {
  return typeof window !== 'undefined' && WEB_RUNTIME_FLAG in window
}

export function webServerBase(): string {
  return typeof window !== 'undefined' && (window as any).__PYMSS_WEB_BASE__ ? String((window as any).__PYMSS_WEB_BASE__) : ''
}

const invokeQueues = new Map<string, Promise<unknown>>()

/** Serialize invokes per command name, mirroring Tauri's ordered IPC for a command. */
function enqueueInvoke<T>(command: string, action: () => Promise<T>): Promise<T> {
  const previous = invokeQueues.get(command) ?? Promise.resolve()
  const next = previous.catch(() => undefined).then(action)
  invokeQueues.set(command, next)
  void next.catch(() => undefined).finally(() => {
    if (invokeQueues.get(command) === next) invokeQueues.delete(command)
  })
  return next as Promise<T>
}

export async function webFetch(path: string, init?: RequestInit): Promise<Response> {
  return fetch(webServerBase() + path, {
    ...init,
    headers: { ...(init?.headers || {}), 'x-pymss-session': sessionId || '' },
    credentials: 'same-origin',
  })
}

let sessionId = ''
let sessionInfo: WebSessionInfo | null = null
export function webSession(): WebSessionInfo | null { return sessionInfo }
export function webSessionId(): string { return sessionId }

// --- upload picker ------------------------------------------------------------
const AUDIO_EXTS = ['wav', 'mp3', 'flac', 'm4a', 'aac', 'ogg', 'opus']
const VIDEO_EXTS = ['mp4', 'mkv', 'mov', 'avi', 'webm', 'flv']
const WEIGHT_EXTS = ['ckpt', 'pth', 'th', 'pt', 'bin']

function extOf(name: string) {
  const parts = name.split('.')
  return parts.length > 1 ? parts.pop()!.toLowerCase() : ''
}

function acceptFor(options: any): string {
  if (options?.directory) return ''
  const exts = [...AUDIO_EXTS, ...VIDEO_EXTS, ...WEIGHT_EXTS]
  return exts.map((ext) => `.${ext}`).join(',')
}

async function pickWithInput(options: any): Promise<string | string[] | null> {
  return new Promise((resolve) => {
    const input = document.createElement('input')
    input.type = 'file'
    if (options?.multiple) input.multiple = true
    const accept = acceptFor(options)
    if (accept) input.accept = accept
    // Folder picking is impossible in a browser; force file picking.
    input.style.display = 'none'
    document.body.appendChild(input)
    let settled = false
    const finish = (value: string | string[] | null) => {
      if (settled) return
      settled = true
      window.removeEventListener('focus', onFocus, true)
      input.remove()
      resolve(value)
    }
    const onFocus = () => {
      // The picker closes -> focus returns. If no change event arrives shortly
      // after, treat it as a cancel (matches plugin-dialog's null contract).
      window.setTimeout(() => {
        if (!settled && !input.files?.length) finish(null)
      }, 400)
    }
    input.addEventListener('change', async () => {
      const files = [...(input.files || [])]
      if (!files.length) return finish(null)
      try {
        const form = new FormData()
        for (const file of files) form.append('files', file, file.name)
        const response = await webFetch('/api/uploads', { method: 'POST', body: form })
        if (!response.ok) throw new Error(`upload failed: ${response.status}`)
        const payload = (await response.json()) as { paths?: string[] }
        const paths = payload.paths || []
        finish(options?.multiple ? paths : (paths[0] ?? null))
      } catch (error) {
        console.error('[web] upload failed', error)
        finish(null)
      }
    })
    window.addEventListener('focus', onFocus, true)
    input.click()
  })
}

// --- serverless canvas (kept minimal; uploads power the real flows) ------------
let appPathsCache: WebAppPaths | null = null
export async function webAppPaths(): Promise<WebAppPaths> {
  if (appPathsCache) return appPathsCache
  const response = await webFetch('/api/paths')
  if (!response.ok) throw new Error('failed to load web app paths')
  appPathsCache = (await response.json()) as WebAppPaths
  return appPathsCache
}

// --- event hub (SSE) -----------------------------------------------------------
const callbacks = new Map<number, CbEntry>()
const listeners = new Map<number, EventEntry>()
let eventSeq = 1
let sseSource: EventSource | null = null
let sseStarted = false
const onEventCallbacks = new Set<(name: string, payload: unknown) => void>()

export function onWebServerEvent(handler: (name: string, payload: unknown) => void): () => void {
  onEventCallbacks.add(handler)
  return () => onEventCallbacks.delete(handler)
}

function dispatchServerEvent(name: string, payload: unknown) {
  for (const handler of [...onEventCallbacks]) {
    try { handler(name, payload) } catch (error) { console.error('[web] event handler failed', error) }
  }
}

function dispatchEvent(id: number, event: string, payload: unknown) {
  const entry = callbacks.get(id)
  entry?.handler?.({ id, event, payload })
  if (entry?.once) callbacks.delete(id)
}

export async function startWebEventStream(): Promise<void> {
  if (sseStarted) return
  sseStarted = true
  const source = new EventSource(webServerBase() + `/api/events?session=${encodeURIComponent(sessionId)}`)
  sseSource = source
  source.onmessage = (message) => {
    let envelope: any
    try { envelope = JSON.parse(message.data) } catch { return }
    if (envelope?.kind === 'callback') {
      dispatchEvent(envelope.id, envelope.event, envelope.payload)
    } else if (envelope?.kind === 'session') {
      // session refresh (e.g. after server restart)
    } else if (envelope?.kind === 'named') {
      dispatchServerEvent(envelope.event, envelope.payload)
    }
  }
  source.onerror = () => {
    // EventSource auto-reconnects; surface the status for diagnostics.
    dispatchServerEvent('pymss://web-events-status', { state: source.readyState })
  }
}

export function transformCallback(handler?: (value: any) => void, once = false): number {
  const id = eventSeq++
  callbacks.set(id, { handler, once })
  return id
}

async function listenEvent(event: string, handler: number, target?: string): Promise<number> {
  await startWebEventStream()
  const id = eventSeq++
  listeners.set(id, { event, handler, target })
  return id
}

function unlistenEvent(eventId: number) {
  listeners.delete(eventId)
}

// fan out server broadcasts to plugin:event listeners
export function registerNamedDispatch() {
  onWebServerEvent((name, payload) => {
    for (const [, entry] of [...listeners]) {
      if (entry.event === name) dispatchEvent(entry.handler, name, payload)
    }
  })
}

// --- invoke routing ------------------------------------------------------------
async function invokeHttp<T>(command: string, args?: any): Promise<T> {
  return enqueueInvoke(command, async () => {
    const response = await webFetch(`/api/invoke/${command}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(args ?? {}),
    })
    if (response.status === 401) {
      location.href = webServerBase() + '/login'
      return new Promise<T>(() => {})
    }
    const text = await response.text()
    let payload: any = null
    try { payload = text ? JSON.parse(text) : null } catch { payload = text }
    if (!response.ok) {
      const message = typeof payload === 'string' ? payload
        : payload?.message || payload?.error || `invoke ${command} failed: ${response.status}`
      throw new Error(String(message))
    }
    return payload as T
  })
}

async function invokeBinary(command: string, args: any): Promise<ArrayBuffer> {
  const response = await webFetch(`/api/binary/${command}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(args ?? {}),
  })
  if (!response.ok) {
    let message = `invoke ${command} failed: ${response.status}`
    try {
      const payload = await response.json()
      if (payload?.message || payload?.error) message = String(payload.message || payload.error)
    } catch { /* binary error path */ }
    throw new Error(message)
  }
  return response.arrayBuffer()
}

const BINARY_COMMANDS = new Set(['editor_preview_chunk'])

const PICK_COMMANDS = new Set(['pick_media_files', 'pick_audio_files', 'pick_single_audio_file', 'pick_input_folder', 'pick_output_folder', 'pick_model_weights_file', 'pick_model_config_file'])

async function invokeDispatch<T>(command: string, args?: any): Promise<T> {
  if (command === 'plugin:event|listen') {
    return (await listenEvent(args.event, args.handler, args.target?.label)) as unknown as T
  }
  if (command === 'plugin:event|unlisten') { unlistenEvent(args.eventId); return undefined as unknown as T }
  if (command === 'plugin:event|emit') { dispatchServerEvent(args.event, args.payload); return undefined as unknown as T }
  if (command === 'plugin:event|emit_to') {
    // Cross-tab routing: the server relays labelled events over SSE; same-tab
    // delivery is immediate. Web mode uses one window per editor (tab), so
    // emitting to 'main' also notifies this tab's listeners directly.
    dispatchServerEvent(args.event, args.payload)
    await invokeHttp('plugin:event|emit_to', { event: args.event, payload: args.payload, target: args.target })
    return undefined as unknown as T
  }
  if (command === 'plugin:dialog|open') return (await pickWithInput(args?.options)) as unknown as T
  if (PICK_COMMANDS.has(command)) {
    // Native picker + upload, executed client-side for instant feedback.
    const multiple = command === 'pick_media_files' || command === 'pick_audio_files'
    const picked = await pickWithInput({ multiple })
    return (picked || null) as unknown as T
  }
  if (command === 'plugin:dialog|save') {
    // Browser cannot choose a server-side path; download instead.
    const content = String(args?.options?.content ?? '')
    const name = String(args?.options?.defaultPath || 'pymss-export.txt')
    const url = URL.createObjectURL(new Blob([content], { type: 'text/plain' }))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = name
    anchor.click()
    URL.revokeObjectURL(url)
    return null as unknown as T
  }
  if (command === 'plugin:shell|open') {
    if (typeof args?.path === 'string' && /^https?:\/\//i.test(args.path)) window.open(args.path, '_blank', 'noopener')
    return undefined as unknown as T
  }
  if (command === 'plugin:process|exit') {
    window.close()
    return undefined as unknown as T
  }
  if (command === 'plugin:window|close' || command === 'plugin:window|destroy') { window.close(); return undefined as unknown as T }
  if (command === 'plugin:window|minimize' || command === 'plugin:window|hide') return undefined as unknown as T
  if (command === 'plugin:window|show' || command === 'plugin:window|set_focus' || command === 'plugin:window|unminimize') return undefined as unknown as T
  if (command === 'plugin:window|is_maximized' || command === 'plugin:window|is_minimized') return false as unknown as T
  if (command === 'plugin:window|is_focused' || command === 'plugin:window|is_visible' || command === 'plugin:window|is_decorated') return true as unknown as T
  if (command === 'plugin:window|start_dragging') return undefined as unknown as T
  if (command === 'plugin:window|set_title') { document.title = String(args?.title || document.title); return undefined as unknown as T }
  if (command === 'plugin:webview|set_webview_zoom') {
    // apply the zoom factor to the document, mirroring the desktop scale behavior
    const factor = Number(args?.zoom)
    if (Number.isFinite(factor) && factor > 0) document.documentElement.style.zoom = String(factor)
    return undefined as unknown as T
  }
  if (BINARY_COMMANDS.has(command)) return (await invokeBinary(command, args)) as unknown as T
  return invokeHttp<T>(command, args)
}

// --- installation ---------------------------------------------------------------
export async function installWebRuntime(base = '') {
  if (typeof window === 'undefined') return
  ;(window as any).__PYMSS_WEB_BASE__ = base
  ;(window as any)[WEB_RUNTIME_FLAG] = true
  registerNamedDispatch()
  const internals = {
    invoke: (cmd: string, args?: any, options?: any) => invokeDispatch(cmd, args),
    transformCallback,
    convertFileSrc: (path: string) => `${webServerBase()}/api/asset?path=${encodeURIComponent(path)}&session=${encodeURIComponent(sessionId)}`,
    unregisterCallback: (id: number) => { callbacks.delete(id) },
    metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main', windowLabel: 'main' } },
    plugins: {},
  }
  ;(window as any).__TAURI_INTERNALS__ = internals

  // fetch session info first so asset URLs and the event stream carry it
  const info = await (await webFetch('/api/session')).json() as WebSessionInfo
  sessionId = info.sessionId
  sessionInfo = info
  ;(window as any).__TAURI_INTERNALS__.convertFileSrc = (path: string) =>
    `${webServerBase()}/api/asset?path=${encodeURIComponent(path)}&session=${encodeURIComponent(sessionId)}`
  await startWebEventStream()
}
