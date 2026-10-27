#!/usr/bin/env node
/**
 * pymss-studio web server.
 *
 * Serves the built studio UI (dist/) in a plain browser and bridges the browser's
 * Tauri-shaped IPC calls to the same python/worker.py the desktop app spawns:
 *
 *   POST /api/invoke/:command   -> command dispatch (same names as #[tauri::command])
 *   POST /api/binary/:command   -> binary responses (editor_preview_chunk)
 *   GET  /api/events            -> SSE stream of `pymss://worker-event` + app events
 *   POST /api/uploads           -> browser uploads land in <dataRoot>/uploads
 *   GET  /api/asset?path=       -> stream a server-side file (convertFileSrc)
 *   GET  /api/session /api/paths
 *   POST /login  (optional password auth, PYMSS_WEB_PASSWORD / --password)
 *
 * Data layout mirrors the desktop app (PYMSS_STUDIO_DATA_ROOT or ./pymss-data):
 *   settings/ models/ outputs/ editor-projects/ temp/ uploads/ runtime-envs/
 *
 * Usage:
 *   node scripts/web-server.mjs [--port 8710] [--host 127.0.0.1]
 *         [--python /path/to/python] [--data-root DIR] [--dist DIR]
 *         [--password SECRET] [--no-auth-ignore-localhost]
 */
import { createServer } from 'node:http'
import { createReadStream, existsSync, mkdirSync, readFileSync, writeFileSync, statSync, rmSync, readdirSync, renameSync, appendFileSync, openSync, closeSync, unlinkSync, fstatSync, readSync } from 'node:fs'
import { spawn } from 'node:child_process'
import { basename, dirname, extname, join, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID, createHash } from 'node:crypto'
import os from 'node:os'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(__dirname, '..')

// ---------------------------------------------------------------- args
function parseArgs(argv) {
  const options = { port: 8710, host: '127.0.0.1', python: process.env.PYMSS_STUDIO_PYTHON || 'python3', dist: join(ROOT, 'dist'), password: process.env.PYMSS_WEB_PASSWORD || '' }
  for (let index = 0; index < argv.length; index += 1) {
    const value = (name, fallback) => { index += 1; return argv[index] || fallback }
    switch (argv[index]) {
      case '--port': options.port = Number(value()) || options.port; break
      case '--host': options.host = value(); break
      case '--python': options.python = value(); break
      case '--data-root': options.dataRoot = value(); break
      case '--dist': options.dist = value(); break
      case '--password': options.password = value(); break
      default: break
    }
  }
  options.dataRoot = resolve(options.dataRoot || process.env.PYMSS_STUDIO_DATA_ROOT || join(ROOT, 'pymss-data'))
  return options
}
const OPTIONS = parseArgs(process.argv.slice(2))

// ---------------------------------------------------------------- paths
const DATA_ROOT = OPTIONS.dataRoot
const SETTINGS_DIR = join(DATA_ROOT, 'settings')
const MODELS_DIR = join(DATA_ROOT, 'models')
const OUTPUTS_DIR = join(DATA_ROOT, 'outputs')
const EDITOR_PROJECTS_DIR = join(DATA_ROOT, 'editor-projects')
const TEMP_DIR = join(DATA_ROOT, 'temp')
const UPLOADS_DIR = join(DATA_ROOT, 'uploads')
const RUNTIME_ENVS_DIR = join(DATA_ROOT, 'runtime-envs')
const ACTIVE_RUNTIME_FILE = join(RUNTIME_ENVS_DIR, 'active-runtime.json')
const WORKER = join(ROOT, 'python', 'worker.py')

for (const dir of [DATA_ROOT, SETTINGS_DIR, MODELS_DIR, OUTPUTS_DIR, EDITOR_PROJECTS_DIR, TEMP_DIR, UPLOADS_DIR, RUNTIME_ENVS_DIR]) {
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
}

const CRLF = '\r\n'
const CRLF_BUFFER = Buffer.from(CRLF)
const HEADER_SEPARATOR = Buffer.from('\r\n\r\n')

const AUDIO_EXTENSIONS = ['wav', 'mp3', 'flac', 'm4a', 'aac', 'ogg', 'opus']
const VIDEO_EXTENSIONS = ['mp4', 'mkv', 'mov', 'avi', 'webm', 'flv']
const appPaths = () => ({
  dataRoot: DATA_ROOT, settingsDir: SETTINGS_DIR, modelsDir: MODELS_DIR, outputsDir: OUTPUTS_DIR,
  editorProjectsDir: EDITOR_PROJECTS_DIR, logsDir: join(DATA_ROOT, 'logs'), tempDir: TEMP_DIR,
})

// ---------------------------------------------------------------- event bus
class EventBus {
  constructor() { this.clients = new Set() }
  attach(response) {
    const client = { response, buffer: [] }
    this.clients.add(client)
    response.writeHead(200, {
      'content-type': 'text/event-stream',
      'cache-control': 'no-cache, no-transform',
      connection: 'keep-alive',
      'x-accel-buffering': 'no',
    })
    response.write(':connected\n\n')
    const ping = setInterval(() => {
      try { response.write(':ping\n\n') } catch { /* dropped */ }
    }, 15000)
    response.on('close', () => { clearInterval(ping); this.clients.delete(client) })
    return client
  }
  send(client, payload) {
    const chunk = `data: ${JSON.stringify(payload)}\n\n`
    try { client.response.write(chunk) } catch { this.clients.delete(client) }
  }
  /** Broadcast to every connected browser. */
  emitEvent(event, payload) {
    for (const client of [...this.clients]) this.send(client, { kind: 'named', event, payload })
  }
  /** Broadcast a worker envelope exactly like Tauri's `pymss://worker-event`. */
  emitWorker(envelope) { this.emitEvent('pymss://worker-event', envelope) }
}
const bus = new EventBus()

// ---------------------------------------------------------------- session / auth
const SESSIONS = new Map() // token -> { user }
const authEnabled = () => OPTIONS.password !== ''
function checkPassword(password) {
  const expected = OPTIONS.password
  const provided = String(password || '')
  const a = Buffer.from(provided)
  const b = Buffer.from(expected)
  return a.length === b.length && timingSafeEqual(a, b)
}
function timingSafeEqual(a, b) {
  let diff = a.length ^ b.length
  for (let index = 0; index < Math.max(a.length, b.length); index++) diff |= (a[index] || 0) ^ (b[index] || 0)
  return diff === 0
}
function sessionFromRequest(request) {
  const header = request.headers['x-pymss-session']
  if (typeof header === 'string' && SESSIONS.has(header)) return header
  const cookie = String(request.headers.cookie || '')
  const match = cookie.match(/(?:^|;\s*)pymss_session=([a-f0-9-]+)/)
  if (match && SESSIONS.has(match[1])) return match[1]
  return null
}
function isAuthorized(request) {
  return !authEnabled() || sessionFromRequest(request) !== null
}

// ---------------------------------------------------------------- app store
const APP_STORE_NAMES = new Set(['app-settings', 'task-history', 'model-state', 'editor-ui', 'audio-tools', 'workflow-state', 'separate-state', 'update-state'])
const appStorePath = (name) => join(SETTINGS_DIR, `${name}.json`)
function readAppStore(name) {
  const path = appStorePath(name)
  if (!existsSync(path)) return null
  try { return JSON.parse(readFileSync(path, 'utf8')) } catch { return null }
}
function atomicWrite(path, content) {
  const temporary = `${path}.tmp-${process.pid}-${Date.now()}`
  writeFileSync(temporary, content)
  renameSync(temporary, path)
}
function writeAppStore(name, data) {
  atomicWrite(appStorePath(name), JSON.stringify(data, null, 2))
}

// ------------------------------------------------- workflow store (mutation copy)
function workflowStorePath() { return appStorePath('workflow-state') }
function mutateWorkflowStore(mutation) {
  const path = workflowStorePath()
  let state = existsSync(path) ? safeJson(readFileSync(path, 'utf8')) : null
  if (!state || typeof state !== 'object') state = { workflows: [], selectedWorkflowId: '' }
  if (!Array.isArray(state.workflows)) state.workflows = []
  const action = String(mutation?.action || '')
  let conflict = null
  const legacyIndex = (list) => {
    const explicit = Number.isInteger(mutation.legacyIndex) && mutation.legacyIndex >= 0 && mutation.legacyIndex < list.length
      ? mutation.legacyIndex : -1
    const matches = (item) => item && typeof item === 'object' && !String(item.id || '').trim()
    if (explicit >= 0 && matches(list[explicit])) return explicit
    if (mutation.legacyEntry || mutation.legacyIndex !== undefined) {
      const position = list.findIndex(matches)
      if (position >= 0) return position
    }
    return -1
  }
  const selectedBefore = String(state.selectedWorkflowId || '')

  if (action === 'upsert') {
    const entry = mutation.entry && typeof mutation.entry === 'object' ? mutation.entry : null
    if (!entry) throw new CommandError('workflow entry is required')
    const workflowId = String(entry.id || '').trim()
    if (!workflowId) throw new CommandError('workflow id is required')
    let existingIndex = state.workflows.findIndex((item) => item?.id === workflowId)
    const hasLegacyReference = mutation.legacyIndex !== undefined || mutation.legacyEntry !== undefined
    let legacyTargetMismatch = false
    if (existingIndex < 0) {
      existingIndex = legacyIndex(state.workflows)
      if (existingIndex < 0 && hasLegacyReference) legacyTargetMismatch = true
    }
    const actualUpdatedAt = Number(state.workflows[existingIndex]?.updatedAt) || 0
    const expected = mutation.expectedUpdatedAt === undefined ? undefined : Number(mutation.expectedUpdatedAt)
    const force = mutation.force === true
    if (!force && (legacyTargetMismatch || (expected !== undefined && expected !== actualUpdatedAt))) {
      conflict = { workflowId, expectedUpdatedAt: expected || 0, actualUpdatedAt }
    } else {
      if (existingIndex >= 0) {
        const createdAt = Number(state.workflows[existingIndex].createdAt) || 0
        if (createdAt > 0) entry.createdAt = createdAt
      }
      const requested = Number(entry.updatedAt) || 0
      entry.updatedAt = Math.max(requested, actualUpdatedAt + 1)
      if (existingIndex >= 0) state.workflows[existingIndex] = entry
      else state.workflows.push(entry)
      state.workflows.sort((a, b) => (Number(b?.updatedAt) || 0) - (Number(a?.updatedAt) || 0))
      state.selectedWorkflowId = workflowId
    }
  } else if (action === 'delete') {
    const workflowId = String(mutation.workflowId || '')
    let index = state.workflows.findIndex((item) => item?.id === workflowId)
    if (index < 0) index = legacyIndex(state.workflows)
    const hasLegacyReference = mutation.legacyIndex !== undefined || mutation.legacyEntry !== undefined
    const actualUpdatedAt = Number(state.workflows[index]?.updatedAt) || 0
    const expected = mutation.expectedUpdatedAt === undefined ? undefined : Number(mutation.expectedUpdatedAt)
    if ((index < 0 && hasLegacyReference) || (expected !== undefined && expected !== actualUpdatedAt)) {
      conflict = { workflowId, expectedUpdatedAt: expected || 0, actualUpdatedAt }
    } else {
      const removed = index >= 0 ? state.workflows.splice(index, 1)[0] : null
      if (selectedBefore === workflowId || removed?.id === selectedBefore) {
        state.selectedWorkflowId = String(state.workflows[0]?.id || '')
      }
    }
  } else if (action === 'select') {
    const workflowId = String(mutation.workflowId || '')
    let existingIndex = state.workflows.findIndex((item) => item?.id === workflowId)
    const legacy = existingIndex < 0 ? legacyIndex(state.workflows) : -1
    const hasLegacyReference = mutation.legacyIndex !== undefined || mutation.legacyEntry !== undefined
    if (workflowId && existingIndex < 0 && legacy < 0 && hasLegacyReference) {
      conflict = { workflowId, expectedUpdatedAt: 0, actualUpdatedAt: 0 }
    } else {
      if (legacy >= 0) state.workflows[legacy].id = workflowId
      if (!workflowId || existingIndex >= 0 || legacy >= 0) state.selectedWorkflowId = workflowId
    }
  } else {
    throw new CommandError(`unknown workflow mutation: ${action}`)
  }

  if (conflict) return { state, conflict }
  atomicWrite(workflowStorePath(), JSON.stringify(state, null, 2))
  return { state }
}

// ---------------------------------------------------------------- worker runtime
class CommandError extends Error {}

function loadProxyConfig() {
  const settings = readAppStore('app-settings') || {}
  return {
    mode: ['system', 'none', 'custom'].includes(settings.proxyMode) ? settings.proxyMode : 'system',
    url: String(settings.proxyUrl || ''),
    bypass: String(settings.proxyBypass || ''),
  }
}

function activeRuntimePython() {
  if (!existsSync(ACTIVE_RUNTIME_FILE)) return null
  try {
    const record = safeJson(readFileSync(ACTIVE_RUNTIME_FILE, 'utf8'))
    const python = String(record?.pythonPath || '').trim()
    if (!python) return null
    const backend = String(record?.backend || '').trim().toLowerCase()
    if (!['cpu', 'cuda', 'rocm', 'mlx'].includes(backend)) return null
    const resolved = resolve(dirname(ACTIVE_RUNTIME_FILE), python)
    return existsSync(resolved) ? resolved : (existsSync(python) ? resolve(python) : null)
  } catch { return null }
}

function buildWorkerEnv(command) {
  const env = { ...process.env }
  const bootstrap = ['health', 'runtime_info', 'runtime_env_sizes', 'runtime_core_versions', 'install_runtime', 'activate_runtime', 'delete_runtime', 'update_runtime_core'].includes(command)
  const proxy = loadProxyConfig()
  if (command === 'test_connection') {
    // test_connection supplies its own proxy through the payload
  } else if (proxy.mode === 'custom' && proxy.url) {
    const url = normalizeProxyUrl(proxy.url)
    if (url) { env.HTTP_PROXY = url; env.HTTPS_PROXY = url; env.http_proxy = url; env.https_proxy = url }
  } else if (proxy.mode === 'none') {
    env.NO_PROXY = '*'
    env.HTTP_PROXY = ''; env.HTTPS_PROXY = ''; env.http_proxy = ''; env.https_proxy = ''
  }
  const managed = activeRuntimePython()
  const python = bootstrap ? OPTIONS.python : (managed || OPTIONS.python)
  if (managed && !bootstrap) {
    // overlay support is intentionally omitted in web mode (managed envs carry it internally)
  }
  env.PYMSS_STUDIO_PROXY_CONFIG = JSON.stringify(proxy)
  env.PYMSS_STUDIO_LOG_TRANSPORT = 'protocol'
  env.PYTHONDONTWRITEBYTECODE = '1'
  env.PYTHONUTF8 = '1'
  env.PYTHONIOENCODING = 'utf-8'
  env.PYTHONNOUSERSITE = '1'
  env.PYMSS_STUDIO_BOOTSTRAP_PYTHON = OPTIONS.python
  env.PYMSS_STUDIO_DEFAULT_OUTPUT_DIR = OUTPUTS_DIR
  env.PYMSS_STUDIO_RUNTIME_ENVS_DIR = RUNTIME_ENVS_DIR
  env.PYMSS_STUDIO_ACTIVE_RUNTIME_FILE = ACTIVE_RUNTIME_FILE
  env.PIP_CACHE_DIR = join(TEMP_DIR, 'pip-cache')
  env.NUMBA_CACHE_DIR = join(TEMP_DIR, 'numba-cache')
  return { python, env }
}

function normalizeProxyUrl(url) {
  const value = String(url || '').trim()
  if (!value) return ''
  return value.includes('://') ? value : `http://${value}`
}

function requiresBootstrapPython(command) {
  return ['health', 'runtime_info', 'runtime_env_sizes', 'runtime_core_versions', 'install_runtime', 'activate_runtime', 'delete_runtime', 'update_runtime_core'].includes(command)
}

function payloadFile(command, taskId, payload) {
  const path = join(TEMP_DIR, `payload-${command}-${taskId || 'once'}-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2)}.json`)
  writeFileSync(path, JSON.stringify(payload ?? {}))
  return path
}

const TASKS = new Map() // taskId -> { child, registered: string[] }
const TERMINAL_TASKS = new Set()
const CANCELLED_TASKS = new Set()

function envelopeFromLine(line) {
  return safeJson(line)
}

function runWorkerOnce(command, payload) {
  return new Promise((resolvePromise, reject) => {
    const { python, env } = buildWorkerEnv(command)
    const file = payloadFile(command, null, payload ?? {})
    const child = spawn(python, [WORKER, command, '--payload', file], { env, stdio: ['ignore', 'pipe', 'pipe'] })
    let lastPayload = null
    let sawResult = false
    const stderrLines = []
    let settled = false
    const finish = (error, value) => {
      if (settled) return
      settled = true
      unlinkSync(file)
      if (error) reject(error)
      else resolvePromise(value)
    }
    let stdoutBuffer = ''
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdoutBuffer += chunk
      let newline
      while ((newline = stdoutBuffer.indexOf('\n')) >= 0) {
        const line = stdoutBuffer.slice(0, newline).trim()
        stdoutBuffer = stdoutBuffer.slice(newline + 1)
        if (!line) continue
        const envelope = envelopeFromLine(line)
        if (!envelope || typeof envelope.type !== 'string') {
          if (line.startsWith('__PYMSS_STUDIO_TERMINAL_LOG__')) continue
          stderrLines.push(line.slice(0, 400))
          continue
        }
        const isLog = ['worker_log', 'task_log', 'download_log', 'optional_package_log', 'runtime_install_log', 'runtime_core_update_log'].includes(envelope.type)
        if (!isLog && !sawResult && envelope.type !== 'error') { lastPayload = envelope.payload; sawResult = true }
        // env_info returns over IPC only (desktop does the same to preserve ordering)
        if (command !== 'env_info' || isLog) bus.emitWorker(envelope)
        if (envelope.type === 'error') {
          const message = String(envelope.payload?.message || 'worker error')
          finish(new CommandError(message), null)
        }
      }
    })
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk) => {
      for (const line of chunk.split('\n')) if (line.trim()) stderrLines.push(line.trim().slice(0, 400))
    })
    child.on('error', (error) => finish(error, null))
    child.on('close', (code) => {
      if (settled) return
      if (code === 0 && sawResult) return finish(null, lastPayload)
      if (code === 0 && !sawResult) {
        // Some commands (e.g. activate_runtime) return only logs; treat null payload as success
        return finish(null, lastPayload)
      }
      finish(new CommandError(`worker exited with code ${code}${stderrLines.length ? ': ' + stderrLines.slice(-3).join(' | ') : ''}`), null)
    })
  })
}

function isBackgroundTerminalEvent(command, eventType) {
  switch (command) {
    case 'delete_model': return ['error', 'model_delete_done', 'model_delete_failed'].includes(eventType)
    case 'cleanup_model_residual_files': return ['error', 'model_residual_cleanup_done', 'model_residual_cleanup_failed'].includes(eventType)
    case 'download_model': return ['error', 'download_done', 'task_cancelled'].includes(eventType)
    case 'install_runtime': return ['error', 'runtime_install_finished', 'task_cancelled'].includes(eventType)
    case 'update_runtime_core': return ['error', 'runtime_core_update_finished', 'runtime_core_update_plan', 'task_cancelled'].includes(eventType)
    case 'manage_optional_package': return ['error', 'optional_package_status', 'task_cancelled'].includes(eventType)
    case 'manage_plugin': return ['error', 'plugin_manage_result', 'task_cancelled'].includes(eventType)
    case 'import_custom_model': return ['error', 'custom_model_import_finished', 'task_cancelled'].includes(eventType)
    case 'infer': case 'infer_workflow': return ['error', 'task_done', 'task_cancelled'].includes(eventType)
    case 'audio_tools': return ['error', 'audio_tool_result', 'task_cancelled'].includes(eventType)
    case 'run_operation': return ['error', 'operation_result', 'task_cancelled'].includes(eventType)
    case 'list_nodes': return ['error', 'nodes_list'].includes(eventType)
    case 'export_editor_mix': return ['error', 'editor_mix_exported', 'task_cancelled'].includes(eventType)
    default: return eventType === 'error'
  }
}

function killTree(pid) {
  try { process.kill(-pid, 'SIGKILL') } catch { /* group kill */ }
  try { process.kill(pid, 'SIGKILL') } catch { /* direct */ }
}

function spawnWorkerBackground(command, taskId, payload) {
  for (const id of registeredTaskIds(payload, taskId)) {
    if (TASKS.has(id)) throw new CommandError(`task already exists: ${id}`)
  }
  const { python, env } = buildWorkerEnv(command)
  const file = payloadFile(command, taskId, payload ?? {})
  const child = spawn(python, [WORKER, command, '--payload', file], { env, stdio: ['ignore', 'pipe', 'pipe'], detached: true })
  const registered = registeredTaskIds(payload, taskId)
  const entry = { child, registered }
  for (const id of registered) TASKS.set(id, entry)

  let stdoutBuffer = ''
  const terminal = new Set()
  child.stdout.setEncoding('utf8')
  child.stdout.on('data', (chunk) => {
    stdoutBuffer += chunk
    let newline
    while ((newline = stdoutBuffer.indexOf('\n')) >= 0) {
      const line = stdoutBuffer.slice(0, newline).trim()
      stdoutBuffer = stdoutBuffer.slice(newline + 1)
      if (!line) continue
      const envelope = envelopeFromLine(line)
      if (!envelope || typeof envelope.type !== 'string') continue
      const eventType = envelope.type
      if (isBackgroundTerminalEvent(command, eventType)) {
        if (envelope.taskId && registered.includes(envelope.taskId)) { terminal.add(envelope.taskId); TERMINAL_TASKS.add(envelope.taskId) }
        else if (envelope.requestId && registered.includes(envelope.requestId)) { terminal.add(envelope.requestId); TERMINAL_TASKS.add(envelope.requestId) }
        else if (eventType === 'error') {
          const message = String(envelope.payload?.message || 'worker error')
          for (const id of registered) bus.emitWorker({ type: 'error', taskId: id, payload: { message, logRecord: envelope.payload?.logRecord ?? null } })
          for (const id of registered) { terminal.add(id); TERMINAL_TASKS.add(id) }
        } else { terminal.add(taskId); TERMINAL_TASKS.add(taskId) }
      }
      bus.emitWorker(envelope)
    }
  })
  child.stderr.setEncoding('utf8')
  child.stderr.on('data', (chunk) => {
    for (const line of chunk.split('\n')) {
      const value = line.trim()
      if (value && !value.startsWith('__PYMSS_STUDIO_TERMINAL_LOG__')) {
        bus.emitWorker({ type: 'worker_log', taskId: registered.length === 1 ? registered[0] : null, payload: { message: value.slice(0, 2000), level: 'info', source: 'worker' } })
      }
    }
  })
  const cleanup = () => { for (const id of registered) { if (TASKS.get(id) === entry) TASKS.delete(id); TERMINAL_TASKS.delete(id) } unlinkSync(file) }
  child.on('error', (error) => {
    for (const id of registered) bus.emitWorker({ type: 'error', taskId: id, payload: { message: String(error.message || error), recoverable: true } })
    cleanup()
  })
  child.on('close', (code) => {
    let wasCancelled = false
    for (const id of registered) if (CANCELLED_TASKS.delete(id)) { wasCancelled = true; terminal.add(id) }
    if (wasCancelled && command === 'manage_plugin') {
      for (const id of registered) bus.emitWorker({ type: 'task_cancelled', taskId: id, payload: { message: 'Cancelled' } })
    }
    const missing = registered.filter((id) => !terminal.has(id))
    if (missing.length) {
      if (code !== 0) {
        const message = code === null ? 'worker exited unexpectedly' : `worker exited with code ${code}`
        for (const id of missing) bus.emitWorker({ type: 'error', taskId: id, payload: { message } })
      } else if (['audio_tools', 'export_editor_mix', 'manage_plugin', 'run_operation'].includes(command)) {
        for (const id of missing) bus.emitWorker({ type: 'error', taskId: id, payload: { message: 'worker exited without a result' } })
      } else {
        for (const id of missing) bus.emitWorker({ type: 'task_cancelled', taskId: id, payload: { message: 'Task cancelled' } })
      }
    }
    cleanup()
  })
  return { taskId, started: true }
}

function registeredTaskIds(payload, taskId) {
  const ids = [taskId]
  if (Array.isArray(payload?.tasks)) {
    for (const item of payload.tasks) {
      const id = String(item?.taskId || '')
      if (id && !ids.includes(id)) ids.push(id)
    }
  }
  return ids
}

function cancelTask(taskId) {
  if (TERMINAL_TASKS.has(taskId)) return false
  const entry = TASKS.get(taskId)
  if (!entry) return false
  for (const id of entry.registered) { TASKS.delete(id); CANCELLED_TASKS.add(id) }
  killTree(entry.child.pid)
  for (const id of entry.registered) {
    if (id.startsWith('plugin_manage_')) continue
    bus.emitWorker({ type: 'task_cancelled', taskId: id, payload: { message: 'Cancelled' } })
  }
  return true
}

// ---------------------------------------------------------------- helpers
function safeJson(text) {
  try { return JSON.parse(text) } catch { return null }
}
function nowMillis() { return Date.now() }
function isAudioFile(path) { return AUDIO_EXTENSIONS.includes(extname(path).slice(1).toLowerCase()) }
function isMediaFile(path) {
  const ext = extname(path).slice(1).toLowerCase()
  return AUDIO_EXTENSIONS.includes(ext) || VIDEO_EXTENSIONS.includes(ext)
}
function collectFiles(root, filter, files, warnings, depth = 0) {
  if (depth > 16) return
  let entries
  try { entries = readdirSync(root, { withFileTypes: true }) } catch (error) { warnings.push(`cannot read directory: ${root}`); return }
  for (const entry of entries) {
    const full = join(root, entry.name)
    if (entry.isDirectory()) collectFiles(full, filter, files, warnings, depth + 1)
    else if (entry.isFile() && filter(full)) files.push(full)
  }
}
function scanPaths(paths, { recursive = true, sortFiles = true, media = false } = {}) {
  const filter = media ? isMediaFile : isAudioFile
  const files = []
  const warnings = []
  for (const raw of paths || []) {
    const target = resolve(String(raw))
    let stats = null
    try { stats = statSync(target) } catch { warnings.push(`path not found: ${raw}`); continue }
    if (stats.isFile()) {
      if (filter(target)) files.push(target)
      else warnings.push(`unsupported file: ${target}`)
    } else if (stats.isDirectory()) {
      if (recursive) collectFiles(target, filter, files, warnings)
      else {
        try {
          for (const entry of readdirSync(target, { withFileTypes: true })) {
            const full = join(target, entry.name)
            if (entry.isFile() && filter(full)) files.push(full)
          }
        } catch { warnings.push(`cannot read directory: ${target}`) }
      }
    }
  }
  if (sortFiles) { files.sort(); return { files: [...new Set(files)], warnings } }
  return { files: [...new Set(files)], warnings }
}
function stemFromPath(path) {
  const name = basename(path)
  const dot = name.lastIndexOf('.')
  return dot > 0 ? name.slice(0, dot) : name
}
function stemRank(stem) {
  const lower = stem.toLowerCase()
  if (lower.includes('vocal') || lower.includes('voice')) return 0
  if (lower.includes('instrument') || lower.includes('accompaniment') || lower.includes('karaoke')) return 1
  if (lower.includes('drum')) return 2
  if (lower.includes('bass')) return 3
  if (lower.includes('other')) return 4
  return 9
}
function safeFileName(value) {
  const name = [...String(value)].map((ch) => (/[a-zA-Z0-9\-_.]/.test(ch) ? ch : '_')).join('')
  const trimmed = name.replace(/^_+|_+$/g, '')
  return trimmed ? trimmed.slice(0, 96) : 'project'
}
function validateProjectId(projectId) {
  if (!projectId || projectId === '.' || projectId === '..' || projectId.endsWith('.') || safeFileName(projectId) !== projectId) {
    throw new CommandError('invalid editor project id')
  }
  return projectId
}
const editorProjectDir = (projectId) => join(EDITOR_PROJECTS_DIR, validateProjectId(projectId))
const editorProjectPath = (projectId) => join(editorProjectDir(projectId), 'project.json')
function writeEditorProject(project) {
  const dir = editorProjectDir(String(project?.id || ''))
  mkdirSync(dir, { recursive: true })
  atomicWrite(join(dir, 'project.json'), JSON.stringify(project, null, 2))
  return project
}
function relativePathFromRoot(root, target) {
  const relative = resolve(target).slice(resolve(root).length)
  const parts = relative.split(sep).filter((part) => part && part !== '.' && part !== '..')
  return parts.length ? parts.join('/') : null
}
function linkedAssetFromPath(path, originKind, originRoot) {
  const relative = originRoot ? relativePathFromRoot(originRoot, path) : basename(path)
  return {
    path, name: basename(path), originKind: originKind, originRoot: originRoot || null,
    relativePath: relative, missing: !existsSync(path),
  }
}
function summaryFromProject(project, fallbackId) {
  const id = String(project?.id || '').trim() || fallbackId
  const sourceTaskId = String(project?.sourceTaskId || '').trim() || null
  const sourceResultDir = String(project?.sourceResultDir || '').trim() || null
  return {
    id,
    name: String(project?.name || '').trim() || 'Untitled Project',
    sourceTaskId, sourceResultDir,
    createdAt: Number(project?.createdAt) || 0,
    updatedAt: Number(project?.updatedAt) || 0,
    type: sourceTaskId || sourceResultDir ? 'task' : 'blank',
  }
}

// ------------------------------------------------------ editor preview decode (ffmpeg)
function findFfmpeg() {
  const candidates = [process.env.PYMSS_STUDIO_FFMPEG, '/usr/local/bin/ffmpeg', '/opt/homebrew/bin/ffmpeg', '/usr/bin/ffmpeg', 'ffmpeg']
  for (const candidate of candidates) if (candidate && (candidate === 'ffmpeg' || existsSync(candidate))) return candidate
  return 'ffmpeg'
}
function decodePreviewChunk(ffmpeg, path, index, sampleRate, channels) {
  return new Promise((resolvePromise, reject) => {
    if (!(sampleRate >= 8000 && sampleRate <= 192000) || !(channels >= 1 && channels <= 2) || index > 216000) {
      return reject(new CommandError('Invalid preview chunk parameters'))
    }
    if (!existsSync(path)) return reject(new CommandError('Preview audio file is unavailable'))
    const start = index * 4
    const warm = Math.max(0, start - 1)
    const args = ['-nostdin', '-v', 'error', '-threads', '1', '-copyts', '-start_at_zero']
    if (start > 0) args.push('-ss', String(warm))
    args.push('-i', path, '-map', '0:a:0', '-vn', '-af',
      `atrim=start=${warm},asetpts=PTS-${warm}/TB,aresample=${sampleRate}:first_pts=0,atrim=start=${start - warm}:duration=4,asetpts=PTS-STARTPTS`,
      '-t', '4', '-ar', String(sampleRate), '-ac', String(channels), '-c:a', 'pcm_f32le', '-f', 'f32le', 'pipe:1')
    const child = spawn(ffmpeg, args, { stdio: ['ignore', 'pipe', 'pipe'] })
    const chunks = []
    let stderr = ''
    child.stdout.on('data', (chunk) => chunks.push(chunk))
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk) => { stderr += chunk; if (stderr.length > 4000) stderr = stderr.slice(-2000) })
    child.on('error', reject)
    child.on('close', (code) => {
      if (code !== 0) return reject(new CommandError(`Preview decode failed: ${stderr.trim()}`))
      const data = Buffer.concat(chunks)
      const frameBytes = channels * 4
      if (data.length > 4 * sampleRate * frameBytes || data.length % frameBytes !== 0) {
        return reject(new CommandError('Invalid decoded preview chunk'))
      }
      resolvePromise(data)
    })
  })
}

// ---------------------------------------------------------------- wav helpers
function pcmWavHeader(dataLength, sampleRate, channels) {
  if (!(sampleRate >= 8000 && sampleRate <= 192000) || !(channels >= 1 && channels <= 2)) throw new CommandError('invalid recording audio format')
  const header = Buffer.alloc(44)
  header.write('RIFF', 0)
  header.writeUInt32LE(dataLength + 36, 4)
  header.write('WAVE', 8); header.write('fmt ', 12)
  header.writeUInt32LE(16, 16); header.writeUInt16LE(1, 20); header.writeUInt16LE(channels, 22)
  header.writeUInt32LE(sampleRate, 24); header.writeUInt32LE(sampleRate * channels * 2, 28)
  header.writeUInt16LE(channels * 2, 32); header.writeUInt16LE(16, 34)
  header.write('data', 36); header.writeUInt32LE(dataLength, 40)
  return header
}
function floatWavHeader(frames, sampleRate, channels) {
  if (!frames || !(sampleRate >= 8000 && sampleRate <= 192000) || !(channels >= 1 && channels <= 2)) throw new CommandError('invalid rendered audio parameters')
  const size = frames * channels * 4
  if (size > 0xffffffff - 36) throw new CommandError('rendered audio is too large')
  const header = Buffer.alloc(44)
  header.write('RIFF', 0)
  header.writeUInt32LE(size + 36, 4)
  header.write('WAVE', 8); header.write('fmt ', 12)
  header.writeUInt32LE(16, 16); header.writeUInt16LE(3, 20); header.writeUInt16LE(channels, 22)
  header.writeUInt32LE(sampleRate, 24); header.writeUInt32LE(sampleRate * channels * 4, 28)
  header.writeUInt16LE(channels * 4, 32); header.writeUInt16LE(32, 34)
  header.write('data', 36); header.writeUInt32LE(size, 40)
  return header
}

// ---------------------------------------------------------------- command registry
const proxyStore = { mode: 'system', url: '', bypass: '' }

function validateProxySettings(mode, url) {
  if (!['system', 'none', 'custom'].includes(mode)) throw new CommandError('INVALID_PROXY_MODE: unsupported proxy mode')
  if (mode !== 'custom') return
  const normalized = url.includes('://') ? url.trim() : (url.trim() ? `http://${url.trim()}` : '')
  const schemeSplit = normalized.split('://')
  if (schemeSplit.length < 2) throw new CommandError('INVALID_PROXY_URL: missing proxy scheme')
  const scheme = schemeSplit[0].toLowerCase()
  if (!['http', 'https', 'socks5', 'socks5h'].includes(scheme)) throw new CommandError('UNSUPPORTED_PROXY_SCHEME: unsupported proxy scheme')
  const authority = schemeSplit.slice(1).join('://').split('/')[0]
  const hostPort = authority.split('@').pop() || ''
  let host, port
  if (hostPort.startsWith('[')) {
    const end = hostPort.indexOf(']')
    if (end < 0) throw new CommandError('INVALID_PROXY_URL: invalid IPv6 host')
    host = hostPort.slice(1, end)
    port = hostPort.slice(end + 2)
  } else {
    const lastColon = hostPort.lastIndexOf(':')
    if (lastColon < 0) throw new CommandError('INVALID_PROXY_PORT: proxy port is required')
    host = hostPort.slice(0, lastColon)
    port = hostPort.slice(lastColon + 1)
  }
  if (!host) throw new CommandError('INVALID_PROXY_URL: proxy host is missing')
  const portNumber = Number(port)
  if (!Number.isInteger(portNumber) || portNumber <= 0 || portNumber > 65535) throw new CommandError('INVALID_PROXY_PORT: proxy port is invalid')
}

const COMMANDS = {
  // --- info
  get_build_info: () => ({ version: '0.0.12', gitCommit: '', gitTag: '', gitRef: '', runId: '', runAttempt: '', repository: '', repositoryOwner: '', buildTime: '', target: process.platform, variant: 'web', updateSupported: false, official: false }),
  worker_health: () => runWorkerOnce('health'),
  get_env_info: () => runWorkerOnce('env_info'),
  get_app_paths: () => appPaths(),
  write_frontend_log: () => undefined,
  debug_editor_playback: () => undefined,
  // env check over events
  start_env_check: (args) => {
    const requestId = String(args.requestId || '')
    runWorkerOnce('env_info').then((payload) => {
      bus.emitWorker({ type: 'env_info', requestId, taskId: null, timestamp: null, payload })
    }).catch((error) => {
      bus.emitWorker({ type: 'error', requestId, taskId: null, timestamp: null, payload: { code: 'ENV_CHECK_FAILED', message: String(error.message || error), recoverable: true } })
    })
    return { started: true }
  },
  // --- worker passthrough
  list_models: (args) => runWorkerOnce('list_models', args.payload || null),
  debug_catalog_info: () => runWorkerOnce('debug_catalog_info'),
  debug_catalog_save: (args) => runWorkerOnce('debug_catalog_save', args.payload),
  debug_catalog_reset: () => runWorkerOnce('debug_catalog_reset'),
  debug_model_config: (args) => runWorkerOnce('debug_model_config', args.payload),
  get_model_info: (args) => runWorkerOnce('model_info', args.payload),
  delete_model: (args) => runWorkerOnce('delete_model', args.payload),
  inspect_custom_model: (args) => runWorkerOnce('inspect_custom_model', args.payload),
  unregister_custom_model: (args) => runWorkerOnce('unregister_custom_model', args.payload),
  relink_custom_model: (args) => runWorkerOnce('relink_custom_model', args.payload),
  remap_custom_model_paths: (args) => runWorkerOnce('remap_custom_model_paths', args.payload),
  get_model_storage_summary: (args) => runWorkerOnce('model_storage_summary', args.payload),
  delete_tool_model: (args) => runWorkerOnce('delete_tool_model', args.payload),
  cleanup_model_residual_files: (args) => runWorkerOnce('cleanup_model_residual_files', args.payload),
  download_model: (args) => runWorkerOnce('download_model', args.payload),
  test_proxy_connection: (args) => runWorkerOnce('test_connection', args.payload),
  runtime_info: (args) => runWorkerOnce('runtime_info', args.payload || null),
  runtime_env_sizes: () => runWorkerOnce('runtime_env_sizes'),
  runtime_core_versions: (args) => runWorkerOnce('runtime_core_versions', args.payload || null),
  optional_runtime_package_status: (args) => runWorkerOnce('optional_package_status', { package: args.package }),
  plugin_marketplace: () => runWorkerOnce('plugin_marketplace'),
  get_audio_metadata: (args) => runWorkerOnce('audio_metadata', args.payload),
  list_operations: (args) => runWorkerOnce('list_operations', args.payload || null),
  describe_operation: (args) => runWorkerOnce('describe_operation', args.payload),
  list_nodes: (args) => runWorkerOnce('list_nodes', args.payload || null),
  export_editor_mix: (args) => runWorkerOnce('export_editor_mix', prepareEditorExportPayload(args.payload)),
  generate_waveform_peaks: (args) => {
    const payload = { ...(args.payload || {}) }
    if (!payload.cacheDir) {
      const projectId = String(payload.projectId || 'shared')
      const cacheDir = join(editorProjectDir(projectId), 'peaks')
      mkdirSync(cacheDir, { recursive: true })
      payload.cacheDir = cacheDir
    }
    return runWorkerOnce('waveform_peaks', payload)
  },
  // --- background tasks
  start_separation: (args) => {
    const payload = args.payload || {}
    const taskId = String(payload.taskId || '')
    if (!taskId) throw new CommandError('missing taskId')
    return spawnWorkerBackground('infer', taskId, payload)
  },
  start_workflow_inference: (args) => {
    const payload = args.payload || {}
    const taskId = String(payload.taskId || '')
    if (!taskId) throw new CommandError('missing taskId')
    return spawnWorkerBackground('infer_workflow', taskId, payload)
  },
  start_audio_tool: (args) => {
    const payload = args.payload || {}
    const taskId = String(payload.requestId || '').trim()
    if (!taskId) throw new CommandError('missing audio tool request id')
    return spawnWorkerBackground('audio_tools', taskId, payload)
  },
  start_operation_task: (args) => {
    const payload = args.payload || {}
    const taskId = String(payload.requestId || '').trim()
    if (!taskId) throw new CommandError('missing operation request id')
    return spawnWorkerBackground('run_operation', taskId, payload)
  },
  start_model_download: (args) => {
    const payload = { ...(args.payload || {}) }
    const model = String(payload.model || '')
    if (!model) throw new CommandError('missing model')
    payload.taskId = String(payload.taskId || `download_${model.replace(/[^a-zA-Z0-9]/g, '_')}_${Date.now()}`)
    return spawnWorkerBackground('download_model', payload.taskId, payload)
  },
  start_model_delete: (args) => {
    const payload = { ...(args.payload || {}) }
    const model = String(payload.model || '')
    if (!model) throw new CommandError('missing model')
    payload.taskId = String(payload.taskId || `delete_${model.replace(/[^a-zA-Z0-9]/g, '_')}_${Date.now()}`)
    return spawnWorkerBackground('delete_model', payload.taskId, payload)
  },
  start_custom_model_import: (args) => {
    const payload = { ...(args.payload || {}) }
    const name = String(payload.name || '').trim()
    if (!name) throw new CommandError('missing custom model name')
    for (const id of TASKS.keys()) if (id.startsWith('custom_model_import_')) throw new CommandError('a custom model import is already running')
    payload.taskId = String(payload.taskId || `custom_model_import_${Date.now()}`)
    return spawnWorkerBackground('import_custom_model', payload.taskId, payload)
  },
  start_cleanup_model_residual_files: (args) => {
    const payload = { ...(args.payload || {}) }
    payload.taskId = String(payload.taskId || `cleanup_residual_${Date.now()}`)
    return spawnWorkerBackground('cleanup_model_residual_files', payload.taskId, payload)
  },
  start_runtime_install: (args) => {
    const payload = { ...(args.payload || {}) }
    for (const id of TASKS.keys()) if (id.startsWith('runtime_install_')) throw new CommandError('runtime installation is already running')
    payload.taskId = String(payload.taskId || `runtime_install_${Date.now()}`)
    payload.backend = String(payload.backend || 'cpu')
    return spawnWorkerBackground('install_runtime', payload.taskId, payload)
  },
  start_runtime_core_update: (args) => {
    const payload = { ...(args.payload || {}) }
    payload.taskId = String(payload.taskId || `runtime_core_update_${Date.now()}`)
    return spawnWorkerBackground('update_runtime_core', payload.taskId, payload)
  },
  manage_optional_runtime_package: (args) => {
    const payload = { ...(args.payload || {}) }
    const pkg = String(payload.package || '').trim()
    if (!pkg) throw new CommandError('missing optional runtime package')
    const prefix = `optional_package_${pkg}_`
    for (const id of TASKS.keys()) if (id.startsWith(prefix)) throw new CommandError(`optional package operation is already running: ${pkg}`)
    payload.taskId = String(payload.taskId || `${prefix}${Date.now()}`)
    return spawnWorkerBackground('manage_optional_package', payload.taskId, payload)
  },
  manage_plugin: (args) => {
    const payload = args.payload || {}
    const taskId = String(payload.taskId || `plugin_manage_${Date.now()}`)
    return spawnWorkerBackground('manage_plugin', taskId, payload)
  },
  start_editor_mix_export: (args) => {
    const payload = prepareEditorExportPayload(args.payload || {})
    const taskId = String(payload.taskId || payload.requestId || '').trim()
    if (!taskId) throw new CommandError('missing editor export task id')
    return spawnWorkerBackground('export_editor_mix', taskId, payload)
  },
  cancel_task: (args) => cancelTask(String(args.taskId || '')),
  cancel_runtime_install: (args) => cancelTask(String(args.taskId || '')),
  cancel_runtime_core_update: (args) => cancelTask(String(args.taskId || '')),
  cancel_editor_mix_export: (args) => cancelTask(String(args.taskId || '')),
  // --- runtime management (worker)
  activate_runtime: (args) => runWorkerOnce('activate_runtime', args.payload),
  delete_runtime: (args) => runWorkerOnce('delete_runtime', args.payload),
  // --- proxy
  update_proxy_settings: (args) => {
    const mode = String(args.payload?.mode || 'system')
    const url = String(args.payload?.url || '')
    const bypass = String(args.payload?.bypass || '')
    validateProxySettings(mode, url)
    proxyStore.mode = mode; proxyStore.url = url; proxyStore.bypass = bypass
    return { ok: true, mode }
  },
  // --- app store
  load_app_store: (args) => {
    const name = String(args.name || '')
    if (!APP_STORE_NAMES.has(name)) throw new CommandError(`unknown app store: ${name}`)
    return readAppStore(name)
  },
  save_app_store: (args) => {
    const name = String(args.name || '')
    if (!APP_STORE_NAMES.has(name)) throw new CommandError(`unknown app store: ${name}`)
    if (name === 'workflow-state') throw new CommandError('workflow-state requires mutate_workflow_store')
    writeAppStore(name, args.data ?? null)
    return undefined
  },
  mutate_workflow_store: (args) => {
    const response = mutateWorkflowStore(args.payload || {})
    return { ...response, editorSnapshot: workflowEditors.snapshot() }
  },
  get_workflow_editors: () => workflowEditors.snapshot(),
  // --- picking/scanning (server file system)
  scan_audio_paths: (args) => scanPaths(args.paths, { media: false }),
  scan_audio_paths_with_options: (args) => scanPaths(args.paths, { recursive: args.recursive !== false, sortFiles: args.sortFiles !== false, media: false }),
  scan_media_paths: (args) => scanPaths(args.paths, { media: true }),
  list_audio_files: (args) => scanPaths([args.path], { media: false }).files,
  scan_editor_assets: (args) => scanPaths(args.paths, { media: false }),
  // web-mode pickers use browser upload; these remain for parity
  pick_media_files: () => [],
  pick_audio_files: () => [],
  pick_single_audio_file: () => null,
  pick_input_folder: () => null,
  pick_output_folder: () => null,
  pick_model_weights_file: () => null,
  pick_model_config_file: () => null,
  save_text_file_dialog: () => null,
  reveal_path: () => undefined,
  move_paths_to_trash: (args) => {
    const trashed = []
    const failed = []
    for (const raw of args.paths || []) {
      try {
        const target = resolve(String(raw))
        const withinData = target.startsWith(DATA_ROOT)
        const withinUploads = target.startsWith(UPLOADS_DIR) || target.startsWith(OUTPUTS_DIR)
        if (!withinData && !withinUploads) { failed.push(raw); continue }
        rmSync(target, { recursive: true, force: true })
        trashed.push(raw)
      } catch { failed.push(raw) }
    }
    return { trashed, failed }
  },
  // --- editor projects
  list_editor_projects: () => {
    const items = []
    for (const entry of readdirSync(EDITOR_PROJECTS_DIR, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue
      const projectPath = join(EDITOR_PROJECTS_DIR, entry.name, 'project.json')
      if (!existsSync(projectPath)) continue
      const project = safeJson(readFileSync(projectPath, 'utf8'))
      if (project) items.push(summaryFromProject(project, entry.name))
    }
    items.sort((a, b) => (b.updatedAt - a.updatedAt) || (b.createdAt - a.createdAt) || a.name.localeCompare(b.name))
    return items
  },
  editor_project_exists: (args) => existsSync(editorProjectPath(String(args.projectId || ''))),
  load_editor_project: (args) => {
    const path = editorProjectPath(String(args.projectId || ''))
    if (!existsSync(path)) throw new CommandError('editor project not found')
    return safeJson(readFileSync(path, 'utf8'))
  },
  save_editor_project: (args) => {
    const project = args.project ? { ...args.project, updatedAt: nowMillis() } : args.project
    return writeEditorProject(project)
  },
  create_blank_editor_project: (args) => {
    const payload = args.payload || {}
    const customName = String(payload.name || '').trim()
    const locale = String(payload.locale || 'zh-CN')
    const timestamp = nowMillis()
    const project = {
      version: 2, id: `edit_blank_${timestamp}`,
      name: customName || (locale === 'en' ? 'Untitled Blank Project' : '未命名空工程'),
      masterVolume: 1, sources: [], tracks: [], createdAt: timestamp, updatedAt: timestamp,
    }
    return writeEditorProject(project)
  },
  create_editor_project_from_task: (args) => createEditorProjectFromTask(args.payload || {}),
  delete_editor_project: (args) => {
    const projectId = String(args.projectId || '')
    const dir = editorProjectDir(projectId)
    if (!existsSync(dir)) return false
    rmSync(dir, { recursive: true, force: true })
    return true
  },
  delete_editor_projects_for_tasks: (args) => cleanupEditorProjects(editorProjectIdsForTasks(args.taskIds || []), args.taskIds || []),
  list_open_editor_projects_for_tasks: () => [],
  list_orphaned_editor_projects: (args) => orphanedEditorProjects(args.activeTaskIds || []),
  delete_orphaned_editor_projects: (args) => {
    const orphans = orphanedEditorProjects(args.activeTaskIds || [])
    return cleanupEditorProjects(orphans.map((item) => item.projectId), orphans.map((item) => item.sourceTaskId))
  },
  import_editor_assets: (args) => {
    const imported = []
    const warnings = []
    for (const raw of args.paths || []) {
      const target = resolve(String(raw))
      if (!existsSync(target)) { warnings.push(`path not found: ${raw}`); continue }
      const stats = statSync(target)
      if (stats.isFile()) {
        if (isAudioFile(target)) imported.push(linkedAssetFromPath(target, 'external', dirname(target)))
        else warnings.push(`unsupported file: ${target}`)
      } else if (stats.isDirectory()) {
        for (const file of scanPaths([target]).files) imported.push(linkedAssetFromPath(file, 'external', target))
      }
    }
    imported.sort((a, b) => a.path.localeCompare(b.path))
    const seen = new Set()
    const deduped = imported.filter((item) => (seen.has(item.path) ? false : seen.add(item.path)))
    return { files: deduped, warnings }
  },
  relink_editor_sources: (args) => relinkEditorSources(args.payload || {}),
  // --- editor recording (browser chunks -> server wav)
  begin_editor_recording: (args) => {
    const dir = join(editorProjectDir(String(args.projectId || '')), 'recordings')
    mkdirSync(dir, { recursive: true })
    const base = nowMillis()
    for (let suffix = 0; suffix < 1000; suffix += 1) {
      const recordingId = suffix ? `recording_${base}_${suffix}` : `recording_${base}`
      const partial = join(dir, `${recordingId}.pcm.partial`)
      try {
        closeSync(openSync(partial, 'wx'))
        return { recordingId }
      } catch (error) {
        if (error.code !== 'EEXIST') throw error
      }
    }
    throw new CommandError('could not allocate a recording file')
  },
  append_editor_recording_chunk: (args) => {
    const recordingId = validateRecordingId(args.recordingId)
    const bytes = Buffer.from(String(args.dataBase64 || ''), 'base64')
    if (!bytes.length || bytes.length > 2 * 1024 * 1024 || bytes.length % 2 !== 0) throw new CommandError('invalid recording chunk size')
    const partial = join(editorProjectDir(String(args.projectId || '')), 'recordings', `${recordingId}.pcm.partial`)
    appendFileSync(partial, bytes)
    return undefined
  },
  finish_editor_recording: (args) => {
    const recordingId = validateRecordingId(args.recordingId)
    const dir = join(editorProjectDir(String(args.projectId || '')), 'recordings', )
    const partial = join(dir, `${recordingId}.pcm.partial`)
    if (!existsSync(partial)) throw new CommandError('recording not found')
    const data = readFileSync(partial)
    const sampleRate = Number(args.sampleRate)
    const channels = Number(args.channels)
    if (!data.length || data.length % 2 !== 0 || data.length > 0xffffffff - 36) throw new CommandError('recording does not contain valid PCM audio')
    const header = pcmWavHeader(data.length, sampleRate, channels)
    const output = join(dir, `${recordingId}.wav`)
    const outputPartial = `${output}.partial`
    writeFileSync(outputPartial, Buffer.concat([header, data]))
    renameSync(outputPartial, output)
    unlinkSync(partial)
    return {
      path: output, name: `${recordingId}.wav`,
      duration: data.length / (sampleRate * channels * 2), sampleRate, channels,
    }
  },
  cancel_editor_recording: (args) => {
    const recordingId = validateRecordingId(args.recordingId)
    const dir = join(editorProjectDir(String(args.projectId || '')), 'recordings')
    for (const suffix of ['.pcm.partial', '.wav.partial']) {
      try { unlinkSync(join(dir, recordingId + suffix)) } catch { /* absent */ }
    }
    return undefined
  },
  discard_editor_recording: (args) => {
    const recordingId = validateRecordingId(args.recordingId)
    try { unlinkSync(join(editorProjectDir(String(args.projectId || '')), 'recordings', `${recordingId}.wav`)) } catch { /* absent */ }
    return undefined
  },
  // --- editor export render staging
  append_editor_export_render_chunk: (args) => {
    if (String(args.dataBase64 || '').length > 3 * 1024 * 1024) throw new CommandError('rendered audio chunk is too large')
    const projectId = String(args.projectId || '')
    const taskId = String(args.taskId || '')
    if (!taskId.startsWith('editor_export_') || taskId.length > 96 || !/^[a-zA-Z0-9_-]+$/.test(taskId)) throw new CommandError('invalid editor export task id')
    const directory = join(editorProjectDir(projectId), '.export-render', taskId)
    const frames = Number(args.frames)
    const sampleRate = Number(args.sampleRate)
    const channels = Number(args.channels)
    const offset = Number(args.offset)
    const header = floatWavHeader(frames, sampleRate, channels)
    const bytes = Buffer.from(String(args.dataBase64 || ''), 'base64')
    const alignment = channels * 4
    const size = frames * channels * 4
    if (!bytes.length || bytes.length > 2 * 1024 * 1024 || offset % alignment !== 0 || bytes.length % alignment !== 0 || offset + bytes.length > size) throw new CommandError('invalid rendered audio chunk')
    mkdirSync(directory, { recursive: true })
    const path = join(directory, `clip_${args.clipIndex}.wav`)
    const expected = offset === 0 ? 0 : 44 + offset
    if (offset !== 0 && (!existsSync(path) || statSync(path).size !== expected)) throw new CommandError('rendered audio chunk is out of order')
    if (offset === 0) writeFileSync(path, header)
    appendFileSync(path, bytes)
    const complete = offset + bytes.length === size
    return complete ? {
      path, duration: frames / sampleRate, sampleRate, channels,
    } : null
  },
  cleanup_editor_export_render: (args) => {
    const directory = join(editorProjectDir(String(args.projectId || '')), '.export-render', String(args.taskId || ''))
    if (!String(args.taskId || '').startsWith('editor_export_')) return undefined
    try { rmSync(directory, { recursive: true, force: true }) } catch { /* absent */ }
    return undefined
  },
  // --- binary
  editor_preview_chunk: async (args) => decodePreviewChunk(findFfmpeg(), resolve(String(args.path || '')), Number(args.index), Number(args.sampleRate), Number(args.channels)),
  // --- windows (no-ops in web)
  close_current_window: () => undefined,
  minimize_current_window: () => undefined,
  toggle_maximize_current_window: () => false,
  is_current_window_maximized: () => false,
  start_drag_current_window: () => undefined,
  open_editor_window: (args) => {
    const projectId = String(args.payload?.projectId || '')
    return { projectId, label: `editor-${safeFileName(projectId)}`, opened: true, reused: false, url: `#/editor?projectId=${encodeURIComponent(projectId)}` }
  },
  open_workflow_node_editor_window: (args) => openWorkflowEditorWindow(args, 'node'),
  open_workflow_simple_editor_window: (args) => openWorkflowEditorWindow(args, 'simple'),
  set_plugin_view_root: () => undefined,
  // --- updates: disabled in web mode
  check_managed_update: () => null,
  start_managed_update: () => { throw new CommandError('updates are not available in web mode') },
  get_managed_update_state: () => ({ restartRequired: false }),
  confirm_managed_update_startup: () => undefined,
  // --- logs
  debug_log_info: () => ({ files: [], totalBytes: 0 }),
  debug_log_read: () => ({ records: [], truncated: false }),
  debug_log_clear: () => ({ files: [], totalBytes: 0 }),
  debug_log_create_report: () => ({ path: '', sizeBytes: 0 }),
  debug_runtime_pointers: () => ({ runtimeEnvsDir: RUNTIME_ENVS_DIR, activeRuntimeFile: ACTIVE_RUNTIME_FILE, files: [] }),
  // --- model dir migration: server paths are managed, decline politely
  prepare_model_dir_change: () => ({ changeAllowed: false, reason: 'web' }),
  start_model_dir_migration: () => { throw new CommandError('model directory migration is not available in web mode') },
  respond_model_dir_migration_conflict: () => ({ ok: false }),
  confirm_model_dir_migration_switch: () => ({ ok: false }),
  cancel_model_dir_migration: () => ({ ok: true }),
  // --- tempo/key native: delegated to the worker's audio_tools implementation
  start_tempo_key_analysis: (args) => {
    const payload = args.payload || {}
    const taskId = String(payload.requestId || '').trim()
    if (!taskId) throw new CommandError('missing tempo key request id')
    return spawnWorkerBackground('audio_tools', taskId, { ...payload, operation: 'tempo_key' })
  },
  'plugin:event|emit_to': (args) => {
    // relay labelled event to other tabs through SSE
    bus.emitEvent(String(args?.event || ''), args?.payload)
    return undefined
  },
}

function validateRecordingId(recordingId) {
  const value = String(recordingId || '')
  if (value.startsWith('recording_') && value.length <= 96 && safeFileName(value) === value) return value
  throw new CommandError('invalid recording id')
}

function prepareEditorExportPayload(payload) {
  const result = { ...(payload || {}) }
  const projectId = String(result.project?.id || 'shared')
  if (!result.exportDir) {
    const exportDir = join(editorProjectDir(projectId), 'exports')
    mkdirSync(exportDir, { recursive: true })
    result.exportDir = exportDir
  }
  return result
}

function editorProjectIdsForTasks(taskIds) {
  const seen = new Set()
  return (taskIds || [])
    .map((taskId) => String(taskId || '').trim())
    .filter(Boolean)
    .map((taskId) => `edit_${safeFileName(taskId)}`)
    .filter((projectId) => (seen.has(projectId) ? false : seen.add(projectId)))
}

function cleanupEditorProjects(projectIds, taskIds) {
  const result = { deletedProjectIds: [], missingProjectIds: [], blockedProjectIds: [], failedProjectIds: [] }
  const taskSet = new Set((taskIds || []).map((id) => String(id).trim()).filter(Boolean))
  for (const projectId of projectIds) {
    try {
      validateProjectId(projectId)
    } catch { result.failedProjectIds.push(projectId); continue }
    const dir = join(EDITOR_PROJECTS_DIR, projectId)
    if (!existsSync(dir)) { result.missingProjectIds.push(projectId); continue }
    try {
      rmSync(dir, { recursive: true, force: true })
      result.deletedProjectIds.push(projectId)
    } catch { result.failedProjectIds.push(projectId) }
  }
  void taskSet
  return result
}

function orphanedEditorProjects(activeTaskIds) {
  const active = new Set((activeTaskIds || []).map((id) => String(id).trim()).filter(Boolean))
  const projects = []
  for (const entry of readdirSync(EDITOR_PROJECTS_DIR, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue
    const projectPath = join(EDITOR_PROJECTS_DIR, entry.name, 'project.json')
    if (!existsSync(projectPath)) continue
    const project = safeJson(readFileSync(projectPath, 'utf8'))
    const sourceTaskId = String(project?.sourceTaskId || '').trim()
    if (!sourceTaskId || active.has(sourceTaskId)) continue
    projects.push({
      projectId: entry.name, sourceTaskId,
      name: String(project?.name || '').trim() || entry.name,
    })
  }
  projects.sort((a, b) => a.name.localeCompare(b.name) || a.projectId.localeCompare(b.projectId))
  return projects
}

function createEditorProjectFromTask(payload) {
  const taskId = String(payload.taskId || '')
  const input = String(payload.input || 'Untitled')
  const outputDir = String(payload.outputDir || '')
  let paths = (payload.outputs || [])
    .filter((item) => item?.path)
    .map((item) => ({ stem: String(item.stem || stemFromPath(item.path)), path: item.path }))
  if (!paths.length && outputDir) {
    paths = scanPaths([outputDir]).files.map((path) => ({ stem: stemFromPath(path), path }))
  }
  paths.sort((a, b) => (stemRank(a.stem) - stemRank(b.stem)) || a.stem.localeCompare(b.stem))
  const outputRoot = outputDir ? resolve(outputDir) : null
  const linked = paths.map((item) => {
    const relative = outputRoot ? relativePathFromRoot(outputRoot, item.path) : null
    return { stem: item.stem, asset: { ...linkedAssetFromPath(item.path, 'task-result', outputRoot), relativePath: relative || linkedAssetFromPath(item.path, 'task-result', outputRoot).relativePath } }
  })
  const timestamp = nowMillis()
  const sources = linked.map((item, index) => ({
    id: `source_${index}_${safeFileName(item.stem)}`, role: 'stem', stemKey: item.stem,
    path: item.asset.path, name: item.asset.name, duration: 0, sampleRate: 0, channels: 0,
    peaksPath: null, peaks: [], originKind: 'task-result', originRoot: item.asset.originRoot,
    relativePath: item.asset.relativePath, missing: item.asset.missing,
  }))
  const tracks = linked.map((item, index) => ({
    id: `track_${index}`, sourceId: `source_${index}_${safeFileName(item.stem)}`, role: 'stem',
    name: item.stem, autoName: true, color: null, volume: 1, muted: false, solo: false, fadeIn: 0, fadeOut: 0,
  }))
  const projectId = `edit_${safeFileName(taskId)}`
  const project = {
    version: 2, id: projectId, name: basename(input), sourceTaskId: taskId, sourceResultDir: outputDir,
    masterVolume: 1, sources, tracks, createdAt: timestamp, updatedAt: timestamp,
  }
  return writeEditorProject(project)
}

function relinkEditorSources(payload) {
  const projectId = String(payload.projectId || '')
  const sourceId = String(payload.sourceId || '')
  const pickedPath = resolve(String(payload.pickedPath || ''))
  const path = editorProjectPath(projectId)
  const project = safeJson(readFileSync(path, 'utf8'))
  if (!project) throw new CommandError('editor project not found')
  if (!existsSync(pickedPath)) throw new CommandError('picked relink file does not exist')
  const sources = project.sources || []
  const anchor = sources.find((source) => source.id === sourceId)
  if (!anchor) throw new CommandError('source not found')
  const relinkRoot = deriveRelinkRoot(pickedPath, anchor.relativePath) || dirname(pickedPath)
  const nameIndex = buildFileNameIndex(relinkRoot)
  let relinked = 0
  const unresolved = []
  for (const source of sources) {
    if (!source.missing) continue
    const candidate = buildRelinkCandidate(relinkRoot, source.relativePath, nameIndex)
      || uniqueByName(nameIndex, basename(String(source.path || '')).toLowerCase(), source.relativePath)
    if (!candidate) { unresolved.push(source.id); continue }
    if (source.path !== candidate || source.missing) {
      source.path = candidate
      source.missing = false
      relinked += 1
    }
    source.originRoot = relinkRoot
    if (source.originKind === 'legacy') source.originKind = 'external'
  }
  const saved = writeEditorProject(project)
  return { project: saved, relinked, unresolved }
}
function deriveRelinkRoot(pickedPath, relativePath) {
  if (!relativePath) return null
  const parts = String(relativePath).split(/[\\/]/).filter(Boolean)
  if (!parts.length) return null
  const pickedParts = pickedPath.split(sep)
  if (pickedParts.length < parts.length) return null
  const tail = pickedParts.slice(-parts.length).map((part) => part.toLowerCase())
  if (!tail.every((part, index) => part === parts[index].toLowerCase())) return null
  return pickedParts.slice(0, pickedParts.length - parts.length).join(sep) || '/'
}
function buildFileNameIndex(root) {
  const index = new Map()
  for (const file of scanPaths([root]).files) {
    const name = basename(file).toLowerCase()
    if (!index.has(name)) index.set(name, [])
    index.get(name).push(file)
  }
  return index
}
function uniqueByName(index, name, relativePath) {
  const candidates = index.get(name) || []
  if (candidates.length === 1) return candidates[0]
  if (relativePath && candidates.length > 1) {
    const parts = String(relativePath).split(/[\\/]/).filter(Boolean).map((part) => part.toLowerCase())
    const matches = candidates.filter((candidate) => {
      const candidateParts = candidate.split(sep).map((part) => part.toLowerCase())
      return parts.every((part, index) => candidateParts[candidateParts.length - parts.length + index] === part)
    })
    if (matches.length === 1) return matches[0]
  }
  return null
}
function buildRelinkCandidate(relinkRoot, relativePath, index) {
  if (relativePath) {
    const direct = join(relinkRoot, ...String(relativePath).split(/[\\/]/).filter(Boolean))
    if (existsSync(direct)) return direct
  }
  const name = relativePath ? String(relativePath).split(/[\\/]/).filter(Boolean).pop()?.toLowerCase() : null
  if (!name) return null
  return uniqueByName(index, name, relativePath)
}

// ---- workflow editor registry (web: tabs, so ownership is advisory)
const workflowEditors = {
  revision: 0,
  editors: [],
  snapshot() { return { revision: this.revision, editors: this.editors } },
  bind(label, workflowId, kind) {
    const existing = this.editors.find((item) => item.label === label)
    if (existing && existing.workflowId !== workflowId) {
      existing.workflowId = workflowId
      this.revision += 1
      this.emit()
    }
  },
  release(label) {
    const next = this.editors.filter((item) => item.label !== label)
    if (next.length !== this.editors.length) { this.editors = next; this.revision += 1; this.emit() }
  },
  emit() { bus.emitEvent('pymss://workflow-editors-changed', this.snapshot()) },
}
function openWorkflowEditorWindow(args, kind) {
  const payload = args.payload || {}
  const workflowId = String(payload.workflowId || '').trim()
  const route = kind === 'node' ? 'workflow-node-editor' : 'workflow-simple-editor'
  const label = workflowId ? `${route}-${safeFileName(workflowId)}` : `${route}-new`
  return {
    workflowId, label, kind, opened: true, reused: false,
    url: `#/${route}${workflowId ? `?workflowId=${encodeURIComponent(workflowId)}` : (payload.newWorkflow ? '?new=1' : '')}`,
    editorSnapshot: workflowEditors.snapshot(),
  }
}

// ---------------------------------------------------------------- http server
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf', '.ico': 'image/x-icon',
  '.webp': 'image/webp', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.map': 'application/json',
}

function sendJson(response, status, payload) {
  const body = JSON.stringify(payload)
  response.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' })
  response.end(body)
}

function readBody(request, limit = 64 * 1024 * 1024) {
  return new Promise((resolvePromise, reject) => {
    const chunks = []
    let size = 0
    request.on('data', (chunk) => {
      size += chunk.length
      if (size > limit) { reject(new CommandError('request body too large')); request.destroy(); return }
      chunks.push(chunk)
    })
    request.on('end', () => resolvePromise(Buffer.concat(chunks)))
    request.on('error', reject)
  })
}

function serveStatic(request, response, pathname) {
  let target = pathname === '/' ? '/index.html' : pathname
  const path = join(OPTIONS.dist, target)
  if (!path.startsWith(OPTIONS.dist)) { response.writeHead(403); response.end(); return }
  let file = path
  try {
    const stats = statSync(file)
    if (stats.isDirectory()) file = join(file, 'index.html')
  } catch {
    // SPA fallback to index.html for hash-router deep links without the hash
    file = join(OPTIONS.dist, 'index.html')
  }
  if (!existsSync(file)) {
    response.writeHead(404, { 'content-type': 'text/plain' })
    response.end('dist/ not found — run `pnpm build` first')
    return
  }
  const type = MIME[extname(file)] || 'application/octet-stream'
  response.writeHead(200, { 'content-type': type, 'cache-control': target.startsWith('/assets/') ? 'public, max-age=86400' : 'no-cache' })
  createReadStream(file).pipe(response)
}

function sessionCookie(token) {
  return `pymss_session=${token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${60 * 60 * 24 * 30}`
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url || '/', `http://${request.headers.host || 'localhost'}`)
  const pathname = decodeURIComponent(url.pathname)

  // --- auth gate
  if (pathname === '/login') {
    if (request.method === 'GET') {
      response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
      response.end(loginPage())
      return
    }
    if (request.method === 'POST') {
      const body = new URLSearchParams((await readBody(request, 64 * 1024)).toString('utf8'))
      if (checkPassword(body.get('password'))) {
        const token = randomUUID()
        SESSIONS.set(token, { createdAt: Date.now() })
        response.writeHead(303, { location: '/', 'set-cookie': sessionCookie(token) })
        response.end()
      } else {
        response.writeHead(401, { 'content-type': 'text/html; charset=utf-8' })
        response.end(loginPage('Wrong password'))
      }
      return
    }
  }
  if (!isAuthorized(request)) {
    if (pathname.startsWith('/api/')) return sendJson(response, 401, { message: 'authentication required' })
    response.writeHead(303, { location: '/login' })
    response.end()
    return
  }

  try {
    // --- api
    if (pathname === '/api/session' && request.method === 'GET') {
      return sendJson(response, 200, { sessionId: sessionFromRequest(request) || '', authRequired: authEnabled(), version: '0.0.12' })
    }
    if (pathname === '/api/paths' && request.method === 'GET') {
      return sendJson(response, 200, appPaths())
    }
    if (pathname === '/api/events' && request.method === 'GET') {
      bus.attach(response)
      return
    }
    if (pathname === '/api/uploads' && request.method === 'POST') {
      const contentType = String(request.headers['content-type'] || '')
      const boundaryMatch = contentType.match(/boundary=(?:"([^"]+)"|([^;]+))/)
      if (!boundaryMatch) return sendJson(response, 400, { message: 'multipart form data required' })
      const boundary = Buffer.from(`--${(boundaryMatch[1] || boundaryMatch[2]).trim()}`)
      const body = await readBody(request, 8 * 1024 * 1024 * 1024)
      const paths = []
      let cursor = body.indexOf(boundary)
      if (cursor < 0) return sendJson(response, 400, { message: 'invalid multipart body' })
      const batch = Date.now()
      let batchSequence = 0
      while (cursor >= 0 && cursor < body.length) {
        // cursor -> '--boundary\r\nheaders\r\n\r\ndata\r\n--boundary...'
        let lineEnd = body.indexOf(CRLF, cursor)
        if (lineEnd < 0) break
        const headerStart = lineEnd + CRLF.length
        const headerEnd = body.indexOf(HEADER_SEPARATOR, headerStart)
        if (headerEnd < 0) break
        const dataStart = headerEnd + HEADER_SEPARATOR.length
        // find the next boundary marker: '\r\n--boundary'
        const marker = Buffer.concat([CRLF_BUFFER, boundary])
        let dataEnd = body.indexOf(marker, dataStart)
        if (dataEnd < 0) dataEnd = body.length
        const headerText = body.slice(headerStart, headerEnd).toString('utf8')
        const nameMatch = headerText.match(/filename="([^"]*)"/)
        if (nameMatch?.[1]) {
          const fileName = nameMatch[1].replace(/[\\/:*?"<>|]/g, '_')
          const data = body.slice(dataStart, dataEnd)
          const targetDir = join(UPLOADS_DIR, String(batch))
          mkdirSync(targetDir, { recursive: true })
          const target = join(targetDir, `${batchSequence}_${fileName}`)
          writeFileSync(target, data)
          paths.push(target)
          batchSequence += 1
        }
        cursor = body.indexOf(boundary, dataEnd)
      }
      return sendJson(response, 200, { paths })
    }
    if (pathname.startsWith('/api/invoke/') && request.method === 'POST') {
      const command = pathname.slice('/api/invoke/'.length)
      const body = await readBody(request)
      const args = body.length ? safeJson(body.toString('utf8')) ?? {} : {}
      const handler = COMMANDS[command]
      if (!handler) return sendJson(response, 404, { message: `unknown command: ${command}` })
      const result = await handler(args || {})
      return sendJson(response, 200, result === undefined ? null : result)
    }
    if (pathname.startsWith('/api/binary/') && request.method === 'POST') {
      const command = pathname.slice('/api/binary/'.length)
      const body = await readBody(request)
      const args = body.length ? safeJson(body.toString('utf8')) ?? {} : {}
      const handler = COMMANDS[command]
      if (!handler) return sendJson(response, 404, { message: `unknown command: ${command}` })
      const data = await handler(args || {})
      response.writeHead(200, { 'content-type': 'application/octet-stream', 'cache-control': 'no-store' })
      return response.end(Buffer.isBuffer(data) ? data : Buffer.from(data ?? ''))
    }
    if (pathname === '/api/asset' && request.method === 'GET') {
      const target = resolve(String(url.searchParams.get('path') || ''))
      const allowed = target.startsWith(DATA_ROOT)
      if (!allowed || !existsSync(target) || !statSync(target).isFile()) {
        response.writeHead(404, { 'content-type': 'text/plain' })
        return response.end('not found')
      }
      response.writeHead(200, {
        'content-type': MIME[extname(target)] || 'application/octet-stream',
        'cache-control': 'no-cache',
        'accept-ranges': 'bytes',
      })
      return createReadStream(target).pipe(response)
    }

    // --- static
    if (request.method === 'GET') return serveStatic(request, response, pathname)
    response.writeHead(405)
    response.end()
  } catch (error) {
    const message = error instanceof CommandError ? error.message : String(error?.message || error)
    console.error(`[web] ${request.method} ${pathname} failed:`, message)
    if (pathname.startsWith('/api/')) sendJson(response, 500, { message })
    else { response.writeHead(500); response.end('internal error') }
  }
})

function loginPage(error) {
  return `<!doctype html><html><head><meta charset="utf-8"><title>Pymss Studio</title>
<style>body{font-family:system-ui,sans-serif;background:#101014;color:#e8e8ec;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{background:#1a1a20;border:1px solid #2a2a33;border-radius:12px;padding:32px;width:320px}
input{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #33333d;background:#101014;color:#e8e8ec;margin:12px 0}
button{width:100%;padding:10px;border:0;border-radius:8px;background:#6d5dfc;color:#fff;font-size:14px;cursor:pointer}
.err{color:#ff8080;font-size:13px}</style></head>
<body><form class="card" method="post" action="/login"><h3>Pymss Studio</h3>
${error ? `<p class="err">${error}</p>` : ''}
<input type="password" name="password" placeholder="Access password" autofocus><button>Enter</button></form></body></html>`
}

server.listen(OPTIONS.port, OPTIONS.host, () => {
  console.log(`pymss-studio web server`)
  console.log(`  UI:       http://${OPTIONS.host}:${OPTIONS.port}`)
  console.log(`  data:     ${DATA_ROOT}`)
  console.log(`  worker:   ${WORKER}`)
  console.log(`  python:   ${OPTIONS.python}`)
  console.log(`  auth:     ${authEnabled() ? 'password' : 'disabled (set --password)'}`)
})

process.on('SIGINT', () => {
  console.log('\nshutting down…')
  for (const [, entry] of TASKS) { try { killTree(entry.child.pid) } catch { /* exiting */ } }
  process.exit(0)
})
