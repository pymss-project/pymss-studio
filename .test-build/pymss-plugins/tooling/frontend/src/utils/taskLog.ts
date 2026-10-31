export function getLogLineLevel(line: string): 'debug' | 'info' | 'warn' | 'error' {
  const text = line.replace(/\x1b\[[0-9;]*m/g, '').trimStart()
  const prefix = text.match(/^\[(trace|debug|info|warn|warning|error|fatal|critical)\]\s*/i)
    || text.match(/^(trace|debug|info|warn|warning|error|fatal|critical):\s*/i)
  const message = prefix ? text.slice(prefix[0].length).trimStart() : text
  const embedded = message.match(/^(?:\d{2}:\d{2}:\d{2}\s*\|\s*)?(TRC|TRACE|DBG|DEBUG|INF|INFO|WAR|WRN|WARN|WARNING|ERR|ERROR|FTL|FATAL|CRITICAL)\s*\|/i)
  if (
    !embedded
    && /^traceback(?::|\s+\(most recent call last\):)/i.test(message)
    && (!prefix || /^(warn|warning)$/i.test(prefix[1]))
  ) return 'error'
  const level = (embedded?.[1] || prefix?.[1] || '').toUpperCase()
  if (['TRC', 'TRACE', 'DBG', 'DEBUG'].includes(level)) return 'debug'
  if (['WAR', 'WRN', 'WARN', 'WARNING'].includes(level)) return 'warn'
  if (['ERR', 'ERROR', 'FTL', 'FATAL', 'CRITICAL'].includes(level)) return 'error'
  if (!level && /^[\w.]*?(?:Error|Exception)\s*:/i.test(message)) return 'error'
  return 'info'
}

export function getLogLineLevels(lines: readonly string[]): ReturnType<typeof getLogLineLevel>[] {
  let inTraceback = false
  let tracebackComplete = false
  return lines.map(line => {
    const text = line.replace(/\x1b\[[0-9;]*m/g, '')
    const prefix = text.match(/^\s*(?:\[(trace|debug|info|warn|warning|error|fatal|critical)\]|(trace|debug|info|warn|warning|error|fatal|critical):)[\t ]?/i)
    const message = prefix ? text.slice(prefix[0].length) : text
    const trimmed = message.trimStart()
    const explicitLevel = (prefix?.[1] || prefix?.[2] || '').toLowerCase()
    const errorEnvelope = ['', 'warn', 'warning', 'error', 'fatal', 'critical'].includes(explicitLevel)
    const startsTraceback = errorEnvelope && /^traceback(?::|\s+\(most recent call last\):)/i.test(trimmed)
    const chainedException = /^(?:During handling of the above exception, another exception occurred:|The above exception was the direct cause of the following exception:)/i.test(trimmed)
    const frame = /^(?:File\s+["'].*["'],\s+line\s+\d+\b|[~^]+\s*$)/i.test(trimmed)
    const exceptionSummary = !/^\s/.test(message)
      && /^(?:[a-z_][\w.]+:|KeyboardInterrupt\b|SystemExit\b)/i.test(trimmed)
    const wrappedTraceback = inTraceback && errorEnvelope && (
      chainedException || (!tracebackComplete && (frame || exceptionSummary || /^\s/.test(message)))
    )
    const newLog = (Boolean(prefix) && !wrappedTraceback && !startsTraceback)
      || /^(?:\d{2}:\d{2}:\d{2}\s*\||(?:TRC|TRACE|DBG|DEBUG|INF|INFO|WAR|WRN|WARN|WARNING|ERR|ERROR|FTL|FATAL|CRITICAL)\s*\|)/i.test(trimmed)
    const continuesTraceback = inTraceback && !newLog && (
      /^\s/.test(message) || frame || exceptionSummary || chainedException
    )
    inTraceback = startsTraceback || continuesTraceback
    tracebackComplete = inTraceback && !startsTraceback && !chainedException && (tracebackComplete || exceptionSummary)
    return inTraceback ? 'error' : getLogLineLevel(line)
  })
}
