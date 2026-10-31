export type Metric = { key: string; text: string; unit: string }
const fields: Record<string, Array<[string, string, number]>> = {
  'pymss.analysis.measure_lufs': [['lufs', 'LUFS', 2]],
  'pymss.analysis.measure_true_peak': [['true_peak_dbtp', 'dBTP', 2], ['sample_peak_dbfs', 'dBFS', 2]],
  'pymss.analysis.measure_rms': [['rms_dbfs', 'dBFS', 2], ['rms_linear', '', 6]],
}
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)

export function measurementRows(operation: string, value: unknown, locale: string): Metric[] | null {
  const expected = fields[operation]
  if (!expected || !record(value) || Object.keys(value).length !== expected.length) return null
  const rows: Metric[] = []
  for (const [key, unit, precision] of expected) {
    const number = value[key]
    let text: string
    if (number === -Infinity || number === '-Infinity' || number === '-inf') text = '−∞'
    else if (number === Infinity || number === 'Infinity' || number === 'inf') text = '+∞'
    else if (typeof number === 'number' && Number.isFinite(number)) text = new Intl.NumberFormat(locale, {
      maximumFractionDigits: precision,
      ...(precision === 6 && number !== 0 && Math.abs(number) < 1e-6 ? { notation: 'scientific', maximumFractionDigits: 2 } as const : {}),
    }).format(number)
    else if (number === null || number === 'NaN' || (typeof number === 'number' && Number.isNaN(number))) text = '—'
    else return null
    rows.push({ key, text, unit })
  }
  return rows
}

export type Spectrum = { points: string; maximumFrequency: number; minimumDb: number; maximumDb: number }
export function spectrumChart(value: unknown): Spectrum | null {
  if (!record(value) || !Array.isArray(value.freqs) || !Array.isArray(value.magnitudes_db) || value.freqs.length !== value.magnitudes_db.length || value.freqs.length < 2) return null
  const frequencies = value.freqs, magnitudes = value.magnitudes_db
  if (!frequencies.every((value, index) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && (index === 0 || value > frequencies[index - 1])) || !magnitudes.every(value => typeof value === 'number' && Number.isFinite(value))) return null
  const maximumFrequency = frequencies[frequencies.length - 1]
  if (maximumFrequency <= 0) return null
  const minimumDb = Math.floor(magnitudes.reduce((minimum, value) => Math.min(minimum, value), Infinity) / 10) * 10
  const maximumDb = Math.max(minimumDb + 10, Math.ceil(magnitudes.reduce((maximum, value) => Math.max(maximum, value), -Infinity) / 10) * 10)
  if (!Number.isFinite(maximumDb - minimumDb) || maximumDb <= minimumDb) return null
  // Keep the strongest bin in each pixel group so narrow peaks remain visible.
  const points = []
  const stride = Math.max(1, Math.ceil(frequencies.length / 600))
  for (let start = 0; start < frequencies.length; start += stride) {
    let selected = start
    for (let index = start + 1; index < Math.min(start + stride, frequencies.length); index++) if (magnitudes[index] > magnitudes[selected]) selected = index
    const x = 50 + frequencies[selected] / maximumFrequency * 700
    const y = 20 + (maximumDb - magnitudes[selected]) / (maximumDb - minimumDb) * 200
    points.push(`${x.toFixed(2)},${y.toFixed(2)}`)
  }
  return { points: points.join(' '), maximumFrequency, minimumDb, maximumDb }
}

export function reportText(value: unknown): string {
  if (typeof value === 'string') return value
  try { return JSON.stringify(value, (_, item) => typeof item === 'number' && !Number.isFinite(item) ? String(item) : item, 2) ?? '' }
  catch { return String(value) }
}
