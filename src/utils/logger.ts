import { invoke } from '@tauri-apps/api/core'
import type { LogLevel, LogRecord } from './logRecord.ts'

type Method = 'debug' | 'info' | 'warn' | 'error'

const FEATURE_PAGES = new Set([
  'separate', 'models', 'workflows', 'workflow-node-editor', 'workflow-simple-editor',
  'results', 'tools', 'debug', 'editor', 'settings',
  'plugins', 'plugin-workspace',
])
const AUDIO_TOOLS = new Set(['convert', 'merge', 'sdr', 'midi', 'inspect', 'slicer', 'asr', 'loudness', 'tempo_key'])

export function recordFeatureVisit(page: string, tool?: string) {
  if (!FEATURE_PAGES.has(page) || (tool !== undefined && (page !== 'tools' || !AUDIO_TOOLS.has(tool)))) return
  if (typeof window === 'undefined' || !('__TAURI_INTERNALS__' in window)) return
  const record: LogRecord = {
    schemaVersion: 1, id: `ui-${Date.now()}-${Math.random().toString(36).slice(2)}`,
    timestamp: new Date().toISOString(), source: 'studio', level: 'info',
    logger: 'ui.navigation', event: 'ui.navigation',
    message: tool ? 'Audio tool selected' : 'Page opened',
    context: { page, ...(tool ? { tool } : {}) },
  }
  try { void invoke('write_frontend_log', { record }).catch(() => {}) } catch { /* Navigation remains available when logging is unavailable. */ }
}

const CREDENTIAL_KEYS = 'password|passwd|access_token|refresh_token|token|secret|credential|authorization|api[_-]?key|proxy[-_]?pass'
const SENSITIVE_KEY = new RegExp(`\\b(?:${CREDENTIAL_KEYS})\\b`, 'i')
const MAX_TEXT_NESTING = 8

function decodeQuoted(literal: string): string | undefined {
  if (literal[0] === '"') {
    try { return JSON.parse(literal) } catch { return undefined }
  }
  // Decode string data only; evaluating JavaScript here would execute log contents.
  const escapes: Record<string, string> = { '\\': '\\', "'": "'", '"': '"', n: '\n', r: '\r', t: '\t', b: '\b', f: '\f', v: '\v', a: '\x07' }
  let decoded = ''
  for (let at = 1; at < literal.length - 1; at += 1) {
    const char = literal[at]!
    if (char !== '\\') { decoded += char; continue }
    const escaped = literal[++at]!
    if (Object.hasOwn(escapes, escaped)) decoded += escapes[escaped]
    else if ((escaped === 'u' || escaped === 'x') && new RegExp(`^[0-9a-f]{${escaped === 'u' ? 4 : 2}}$`, 'i').test(literal.slice(at + 1, at + (escaped === 'u' ? 5 : 3)))) {
      const digits = escaped === 'u' ? 4 : 2
      decoded += String.fromCharCode(parseInt(literal.slice(at + 1, at + 1 + digits), 16))
      at += digits
    } else return undefined
  }
  return decoded
}

function encodeSingleQuoted(value: string): string {
  return `'${value.replace(/[\\'\x00-\x1f]/g, char => char === '\\' || char === "'" ? `\\${char}` : `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`)}'`
}

function redactSerializedStrings(value: string, depth: number): string {
  const output: string[] = []
  let copied = 0
  for (let at = 0; at < value.length; at += 1) {
    const quote = value[at]
    if ((quote !== "'" && quote !== '"') || value[at - 1] === '\\') continue
    if (at > 0 && /[\p{L}\p{N}_]/u.test(value[at - 1]!)) {
      const first = value.slice(at + 1).trimStart()[0]
      if (!first || !'{["\'\\'.includes(first)) continue
    }
    let end = at + 1
    while (end < value.length && value[end] !== quote) end += value[end] === '\\' && end + 1 < value.length ? 2 : 1
    if (end === value.length) break
    const decoded = decodeQuoted(value.slice(at, end + 1))
    // Bare JSON keys must retain their names so the assignment pass can mask their values.
    if (decoded !== undefined && SENSITIVE_KEY.test(decoded) && /[=:]/.test(decoded)) {
      const clean = depth >= MAX_TEXT_NESTING ? '<redacted>' : redact(decoded, depth + 1)
      if (clean !== decoded) {
        output.push(value.slice(copied, at), quote === '"' ? JSON.stringify(clean) : encodeSingleQuoted(clean))
        copied = end + 1
      }
    }
    at = end
  }
  output.push(value.slice(copied))
  return output.join('')
}

function redact(value: string, depth = 0): string {
  value = redactSerializedStrings(value, depth).replace(/([a-z][a-z0-9+.-]*:\/\/)[^\s/@]+@/gi, '$1')
  const assignments = new RegExp(`\\b(${CREDENTIAL_KEYS})['"]?\\s*[=:]\\s*`, 'gi')
  const output: string[] = []
  let copied = 0
  for (const match of value.matchAll(assignments)) {
    if (match.index < copied) continue
    let start = match.index + match[0].length
    let end = start
    const quote = value[start] === "'" || value[start] === '"' ? value[start] : undefined
    if (quote) {
      start += 1
      end = start
      while (end < value.length && value[end] !== quote) end += value[end] === '\\' && end + 1 < value.length ? 2 : 1
    } else if (match[1]!.toLowerCase() === 'authorization') {
      while (end < value.length && !/[\r\n]/.test(value[end]!)) end += 1
    } else {
      while (end < value.length && !/[\s,;&}\]'\"]/.test(value[end]!)) end += 1
    }
    output.push(value.slice(copied, start), '<redacted>')
    copied = end
  }
  output.push(value.slice(copied))
  return output.join('')
}

function errorDetail(value: unknown): string {
  if (value instanceof Error) return value.stack || value.message
  if (typeof value === 'string') return value
  if (value && typeof value === 'object') {
    const failure = value as { message?: unknown; stack?: unknown }
    if (typeof failure.stack === 'string') return failure.stack
    if (typeof failure.message === 'string') return failure.message
  }
  return ''
}

export function redactDiagnosticText(value: unknown) { return redact(String(value)) }

export function createLogger(module: string) {
  const write = (level: Method, message: string, details: unknown[]) => {
    const safeMessage = redact(message).slice(0, 8192)
    const detail = redact(details.map(errorDetail).filter(Boolean).join('\n')).slice(0, 32768)
    console[level](`[Studio/${module}] ${safeMessage}`, ...(detail ? [detail] : []))
    if (typeof window === 'undefined' || !('__TAURI_INTERNALS__' in window)) return
    const record: LogRecord = {
      schemaVersion: 1, id: `ui-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      timestamp: new Date().toISOString(), source: 'studio', level: level as LogLevel,
      logger: module, event: `frontend.${level}`, message: safeMessage,
      detail: detail || undefined,
      context: {},
    }
    // Logging must never interrupt the operation that produced the original error.
    try { void invoke('write_frontend_log', { record }).catch(() => {}) } catch { /* A missing IPC bridge must not fail the original operation. */ }
  }
  return {
    debug: (message: string, ...details: unknown[]) => write('debug', message, details),
    info: (message: string, ...details: unknown[]) => write('info', message, details),
    warn: (message: string, ...details: unknown[]) => write('warn', message, details),
    error: (message: string, ...details: unknown[]) => write('error', message, details),
  }
}
