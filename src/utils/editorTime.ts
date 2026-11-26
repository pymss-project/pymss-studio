// 编辑器时间格式化工具。统一各组件的时间码显示，避免重复实现。

/**
 * 将秒数格式化为 mm:ss.t（分:秒.十分之一秒）。
 * 用于传输条、检查器、资产列表等处的时间显示。
 */
export function formatTime(value: number): string {
  const safe = Number.isFinite(value) && value > 0 ? value : 0
  const minutes = Math.floor(safe / 60)
  const seconds = Math.floor(safe % 60)
  const tenths = Math.floor((safe % 1) * 10)
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${tenths}`
}

/**
 * 将秒数格式化为 mm:ss（整秒），用于标尺刻度等不需要小数的场景。
 */
export function formatTimecode(value: number): string {
  const safe = Number.isFinite(value) && value > 0 ? value : 0
  const minutes = Math.floor(safe / 60)
  const seconds = Math.floor(safe % 60)
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

/** 原生宿主最近一次报告的播放状态，`at` 为收到报告时的 `performance.now()`（毫秒）。 */
export type TransportAnchor = { position: number; duration: number; at: number }

/**
 * 按最近一次报告推算当前播放位置（秒）。宿主两次报告之间，以及宿主主线程被插件窗口阻塞时，
 * 播放指针仍按实际经过的时间推进。循环播放在工程末尾回到开头，否则停在工程末尾。
 */
export function extrapolateTransportPosition(anchor: TransportAnchor, now: number, loop: boolean): number {
  const elapsed = Math.max(0, now - anchor.at) / 1000
  const position = Math.max(0, anchor.position) + elapsed
  if (!(anchor.duration > 0)) return position
  if (loop) return position % anchor.duration
  return Math.min(position, anchor.duration)
}

/** Seconds as m:ss.mmm (or h:mm:ss.mmm), for editable timing fields. */
export function formatTimecodeMs(value: number): string {
  const totalMs = Math.round((Number.isFinite(value) && value > 0 ? value : 0) * 1000)
  const ms = totalMs % 1000
  const totalSeconds = Math.floor(totalMs / 1000)
  const seconds = totalSeconds % 60
  const minutes = Math.floor(totalSeconds / 60) % 60
  const hours = Math.floor(totalSeconds / 3600)
  const tail = `${String(seconds).padStart(2, '0')}.${String(ms).padStart(3, '0')}`
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${tail}` : `${minutes}:${tail}`
}

/** Parses h:mm:ss.mmm, m:ss.mmm or plain seconds; returns null for anything else. */
export function parseTimecode(input: string): number | null {
  const text = input.trim()
  if (!text) return null
  const parts = text.split(':')
  if (parts.length > 3 || parts.some(part => !/^\d+(?:[.,]\d+)?$/.test(part))) return null
  if (parts.slice(0, -1).some(part => /[.,]/.test(part))) return null
  const values = parts.map(part => Number(part.replace(',', '.')))
  if (values.length > 1 && values[values.length - 1] >= 60) return null
  return values.reduce((total, value) => total * 60 + value, 0)
}
