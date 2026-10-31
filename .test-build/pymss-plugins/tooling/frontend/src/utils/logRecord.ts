import { getLogLineLevel, getLogLineLevels } from './taskLog.ts'

export type LogLevel = 'trace' | 'debug' | 'info' | 'warn' | 'error' | 'fatal' | null
export type LogSource = 'studio' | 'worker' | 'pymss' | 'third_party' | 'unknown'
export type LogRecord = {
  schemaVersion: 1
  id: string
  timestamp: string
  source: LogSource
  level: LogLevel
  logger: string
  event: string
  message: string
  detail?: string
  taskId?: string
  requestId?: string
  nodeId?: string
  jobId?: string
  location?: { file?: string; line?: number; function?: string }
  context: Record<string, unknown>
  sequence?: number
  sessionId?: string
  legacy?: boolean
}
export type LogEntry = string | LogRecord

const object = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value)
const text = (value: unknown) => typeof value === 'string' ? value : ''

export function normalizeLogLevel(value: unknown): LogLevel {
  const name = text(value).trim().toLowerCase()
  if (['warn', 'warning', 'war', 'wrn'].includes(name)) return 'warn'
  if (['fatal', 'critical', 'ftl'].includes(name)) return 'fatal'
  if (['error', 'err'].includes(name)) return 'error'
  if (['debug', 'dbg'].includes(name)) return 'debug'
  if (['trace', 'trc'].includes(name)) return 'trace'
  if (['info', 'inf'].includes(name)) return 'info'
  return null
}

function normalizeSource(value: unknown): LogSource {
  if (value === 'app' || value === 'frontend' || value === 'studio') return 'studio'
  if (value === 'worker' || value === 'pymss' || value === 'third_party') return value
  return 'unknown'
}

export function logClock(timestamp: string): string {
  if (!timestamp) return ''
  const epoch = timestamp.match(/^(\d{10})\.(\d+)Z?$/)
  const date = epoch ? new Date(Number(epoch[1]) * 1000 + Number(`0.${epoch[2]}`) * 1000) : new Date(timestamp)
  if (!Number.isFinite(date.getTime())) return timestamp
  return `${date.toLocaleTimeString('en-GB', { hour12: false })}.${String(date.getMilliseconds()).padStart(3, '0')}`
}

export function sourceLabel(source: LogSource): string {
  return { studio: 'Studio', worker: 'Worker', pymss: 'pymss', third_party: 'External', unknown: '—' }[source]
}

export function locationLabel(record: LogRecord): string {
  const location = record.location
  return location?.file ? `${location.file}${location.line ? `:${location.line}` : ''}` : ''
}

export function logContextPreview(record: LogRecord): string {
  const hidden = new Set(['processId', 'thread', 'taskId', 'nodeId', 'jobId', 'requestId', 'operationId', 'taskIds', 'taskIdsOmitted', 'raw', 'configuration', 'parameters', 'project'])
  const context = { ...record.context }
  const parameters = object(context.configuration) ? context.configuration : object(context.parameters) ? context.parameters : {}
  for (const key of ['model', 'modelType', 'workflowName', 'device', 'backend', 'mode', 'outputFormat', 'format']) {
    if (context[key] === undefined && parameters[key] !== undefined) context[key] = parameters[key]
  }
  return Object.entries(context)
    .filter(([key, value]) => !key.startsWith('_') && !hidden.has(key) && value !== null && value !== undefined && value !== '')
    .map(([key, value]) => object(value) && typeof value.total === 'number'
      ? `${key}Count=${value.total}` : `${key}=${typeof value === 'string' ? value : JSON.stringify(value)}`)
    .join(' · ').replace(/\s+/g, ' ').trim()
}

export function logMessagePreview(record: LogRecord): string {
  return [record.message || record.event, logContextPreview(record)].filter(Boolean).join(' · ').replace(/\s+/g, ' ').trim()
}

export function normalizeLogRecord(input: unknown, index = 0): LogRecord {
  if (object(input) && object(input.logRecord)) return normalizeLogRecord(input.logRecord, index)
  if (object(input) && input.schemaVersion === 1) {
    return {
      ...input,
      schemaVersion: 1,
      id: text(input.id) || `record-${index}`,
      timestamp: text(input.timestamp),
      source: normalizeSource(input.source),
      level: normalizeLogLevel(input.level),
      logger: text(input.logger),
      event: text(input.event),
      message: text(input.message),
      detail: text(input.detail) || undefined,
      taskId: text(input.taskId) || undefined,
      requestId: text(input.requestId) || undefined,
      nodeId: text(input.nodeId) || undefined,
      jobId: text(input.jobId) || undefined,
      context: object(input.context) ? input.context : {},
      location: object(input.location) ? input.location as LogRecord['location'] : undefined,
    }
  }
  if (typeof input === 'string') {
    try {
      const decoded: unknown = JSON.parse(input)
      if (object(decoded) && (decoded.schemaVersion === 1 || object(decoded.logRecord))) return normalizeLogRecord(decoded, index)
    } catch { /* Historical text is adapted below. */ }
    return legacyRecord(input, index)
  }
  const value = object(input) ? input : {}
  const record = legacyRecord(text(value.message), index)
  return {
    ...record,
    timestamp: text(value.timestamp) || record.timestamp,
    source: normalizeSource(value.source) !== 'unknown' ? normalizeSource(value.source) : record.source,
    level: normalizeLogLevel(value.level) || record.level,
    detail: text(value.detail) || record.detail,
    taskId: text(value.taskId) || undefined,
    nodeId: text(value.nodeId) || undefined,
    requestId: text(value.requestId) || undefined,
    context: object(value.context) ? value.context : record.context,
  }
}

function legacyRecord(raw: string, index: number): LogRecord {
  const clean = raw.replace(/\x1b\[[0-9;]*m/g, '')
  const envelope = clean.match(/^(\S+)\s+(TRACE|DEBUG|INFO|WARN|ERROR|FATAL|PY)\s+(\S+)(?:\s+(.*))?$/)
  const context: Record<string, string> = {}
  const fields = /([A-Za-z][\w]*)=("(?:\\.|[^"\\])*"|\S+)/g
  if (envelope) for (const match of (envelope[4] || '').matchAll(fields)) {
    try { context[match[1]] = JSON.parse(match[2]) } catch { context[match[1]] = match[2] }
  }
  const event = envelope?.[3] || 'legacy'
  let message = envelope ? context.message || event : clean
  let level = normalizeLogLevel(envelope?.[2])
  let source: LogSource = event.startsWith('worker.') ? 'worker' : envelope ? 'studio' : 'unknown'
  if (envelope?.[2] === 'PY') level = /exception|failed/.test(event) ? 'error' : 'debug'
  const prefix = message.match(/^\s*(?:\[(trace|debug|info|warn|warning|error|fatal|critical)\]|(trace|debug|info|warn|warning|error|fatal|critical):)[\t ]?/i)
  if (prefix) {
    level = normalizeLogLevel(prefix[1] || prefix[2])
    message = message.slice(prefix[0].length)
  }
  const embedded = message.match(/^(?:\d{2}:\d{2}:\d{2}\s*\|\s*)?(TRC|TRACE|DBG|DEBUG|INF|INFO|WAR|WRN|WARN|WARNING|ERR|ERROR|FTL|FATAL|CRITICAL)\s*\|\s*(.*)$/i)
  if (embedded) {
    level = normalizeLogLevel(embedded[1])
    message = embedded[2]
    source = /(?:pymss(?:_core)?[\\/]|separator\.py)/i.test(message) ? 'pymss' : 'third_party'
  }
  const location = message.match(/^([^|]+?\.py):(\d+)\s*\|\s*(.*)$/)
  if (location) message = location[3]
  if (!level && getLogLineLevel(message) === 'error') level = 'error'
  if (envelope && event === 'worker.stderr' && !embedded) {
    source = 'third_party'
    level = /^(Traceback|[\w.]+(?:Error|Exception):)/.test(message.trimStart()) ? 'error' : null
  }
  const { message: _message, detail, taskId, requestId, nodeId, ...rest } = context
  return {
    schemaVersion: 1, id: `legacy-${index}-${raw}`, legacy: true,
    timestamp: envelope?.[1] || '', source, level,
    logger: source === 'pymss' ? 'pymss' : event.split('.').slice(0, -1).join('.'), event,
    message, detail: detail || undefined, taskId, requestId, nodeId, context: rest,
    location: location ? { file: location[1], line: Number(location[2]) } : undefined,
  }
}

export function normalizeLogEntries(entries: readonly LogEntry[]): LogRecord[] {
  const legacyLevels = getLogLineLevels(entries.map(entry => typeof entry === 'string' ? entry : ''))
  return entries.map((entry, index) => {
    const record = normalizeLogRecord(entry, index)
    if (record.legacy && record.event === 'legacy' && legacyLevels[index] === 'error') return { ...record, level: 'error' }
    return record
  })
}

export function logRecordRenderKeys(records: readonly LogRecord[]): string[] {
  const occurrences = new Map<string, number>()
  return records.map(record => {
    const occurrence = occurrences.get(record.id) || 0
    occurrences.set(record.id, occurrence + 1)
    return JSON.stringify([record.id, occurrence])
  })
}

export function formatLogRecord(record: LogRecord): string {
  const prefix = [logClock(record.timestamp), (record.level || '—').toUpperCase(), `[${sourceLabel(record.source)}]`].filter(Boolean).join(' ')
  const scope = [record.taskId && `task=${record.taskId}`, record.nodeId && `node=${record.nodeId}`,
    record.jobId && `job=${record.jobId}`, record.requestId && `request=${record.requestId}`].filter(Boolean).join(' ')
  const metadata: { location?: LogRecord['location']; context?: LogRecord['context'] } = {}
  if (record.location) metadata.location = record.location
  if (Object.keys(record.context).length) metadata.context = record.context
  return [
    `${prefix} ${record.message}${scope ? ` (${scope})` : ''}`,
    record.detail,
    Object.keys(metadata).length ? JSON.stringify(metadata, null, 2) : undefined,
  ].filter(Boolean).join('\n')
}

export function workerLogRecord(event: { type?: string; taskId?: string; timestamp?: string; payload?: Record<string, unknown> }): LogRecord {
  const payload = event.payload || {}
  const record = normalizeLogRecord(payload.logRecord || { ...payload, timestamp: payload.timestamp || event.timestamp })
  return record.legacy ? { ...record, taskId: record.taskId || event.taskId } : record
}

export function localLogRecord(message: string, event = 'task.status', level: LogLevel = 'info', scope: Partial<Pick<LogRecord, 'taskId' | 'nodeId' | 'jobId'>> = {}): LogRecord {
  return {
    schemaVersion: 1, id: `local-${Date.now()}-${Math.random().toString(36).slice(2)}`,
    timestamp: new Date().toISOString(), source: 'studio', level, logger: 'studio.tasks', event, message, context: {}, ...scope,
  }
}
