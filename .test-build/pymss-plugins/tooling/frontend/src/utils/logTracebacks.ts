import type { LogRecord } from './logRecord'

export type LogRecordGroup = {
  record: LogRecord
  records: LogRecord[]
  firstIndex: number
  lastIndex: number
  message: string
}

const tracebackStart = /^Traceback(?::|\s+\(most recent call last\):)/i
const tracebackFrame = /^File\s+["'].*["'],\s+line\s+\d+\b/i
const tracebackChain = /^(?:During handling of the above exception, another exception occurred:|The above exception was the direct cause of the following exception:)$/
const tracebackSummary = /^(?:[a-z_][\w.]*(?::|\s*$)|KeyboardInterrupt\b|SystemExit\b)/i

function scopeKey(record: LogRecord) {
  return JSON.stringify([
    record.source, record.logger, record.event, record.sessionId,
    ...(['taskId', 'nodeId', 'jobId', 'requestId'] as const).map((key) => record[key] || record.context[key] || null),
    ...['operationId', 'processId', 'thread', 'taskScope', 'taskIds'].map((key) => record.context[key] ?? null),
  ])
}

function isFragment(record: LogRecord) {
  return !record.detail
    && record.level !== 'info' && record.level !== 'debug' && record.level !== 'trace'
}

/** Group only adjacent traceback fragments; keep the original records for copy and inspection. */
export function groupLogTracebacks(records: readonly LogRecord[]): LogRecordGroup[] {
  const groups: Array<{ records: LogRecord[]; firstIndex: number; lastIndex: number }> = []
  let active: {
    group: typeof groups[number]
    scope: string
    complete: boolean
    hasFrames: boolean
    chaining: boolean
  } | null = null

  for (const [index, record] of records.entries()) {
    const text = record.message.trimStart()
    const starts = tracebackStart.test(text)
    const frame = tracebackFrame.test(text)
    const chain = tracebackChain.test(text.trimEnd())
    const summary = !/^\s/.test(record.message) && tracebackSummary.test(text)
    const fragment = isFragment(record)
    const scope = scopeKey(record)
    const continues = active && fragment && active.scope === scope && (
      starts ? active.chaining || (!active.hasFrames && !active.complete)
        : chain || !text.trim() || (!active.complete && (frame || /^[~^]+\s*$/.test(text) || /^\s/.test(record.message) || summary))
    )

    if (continues && active) {
      active.group.records.push(record)
      active.group.lastIndex = index
    } else {
      const group = { records: [record], firstIndex: index, lastIndex: index }
      groups.push(group)
      active = fragment && (starts || frame)
        ? { group, scope, complete: false, hasFrames: false, chaining: false }
        : null
    }

    if (!active) continue
    if (starts || chain) {
      active.complete = false
      active.hasFrames = false
      active.chaining = chain
    }
    const lines = record.message.split(/\r?\n/)
    if (lines.some((line) => tracebackFrame.test(line.trimStart()))) active.hasFrames = true
    const lastLine = lines.filter((line) => line.trim()).pop()
    if (lastLine && !/^\s/.test(lastLine) && tracebackSummary.test(lastLine) && !tracebackStart.test(lastLine)) {
      active.complete = true
      active.chaining = false
    }
  }

  return groups.map((group) => ({
    ...group,
    record: group.records[0]!,
    message: group.records.map((record) => record.message).join('\n'),
  }))
}
