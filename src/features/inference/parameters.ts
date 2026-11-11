export type ParameterMode = 'model' | 'custom'
export type NativeParameters = Record<string, number | boolean>
export type ParameterCapability = {
  kind: 'free' | 'lattice' | 'fixed' | 'unsupported' | 'unknown'
  min?: number
  max?: number
  step?: number
  offset?: number
  reason?: string
}
export type ParameterResolutionMetadata = {
  version: 1
  sampleRate?: number
  sampleRateSource?: string
  defaults: NativeParameters
  defaultSources?: Record<string, 'model-config' | 'runtime-default'>
  capabilities: Record<string, ParameterCapability>
  overlap: { kind: 'samples' | 'count' | 'unknown'; count?: number }
  fingerprint: string
}
export type CustomParameterIntent = {
  chunkMilliseconds?: number
  overlapBasisPoints?: number
  batchSize?: number
  standardize?: boolean
  normalize?: boolean
  vr?: Partial<Record<'window_size' | 'aggression' | 'post_process_threshold' | 'enable_post_process' | 'high_end_process', number | boolean>>
}
export type ParameterSource = 'model-config' | 'runtime-default' | 'model-library' | 'custom'
export type ParameterIssue = { key: string; code: 'metadataUnavailable' | 'invalidValue' | 'sampleRateMissing' | 'noLegalChunk' }
export type ResolvedParameters = {
  parameters: NativeParameters
  sources: Record<string, ParameterSource>
  adjustments: Record<string, { requested: number; actual: number }>
  skipped: string[]
  issues: ParameterIssue[]
}
export type ResolvedInferenceSnapshot = {
  version: 1
  fingerprint: string
  parameters: NativeParameters
  useTta: boolean
  mode: ParameterMode
  intent: CustomParameterIntent
  sources: Record<string, ParameterSource>
  adjustments: ResolvedParameters['adjustments']
}

const NUMBER_KEYS = new Set(['batch_size', 'overlap_size', 'num_overlap', 'chunk_size', 'window_size', 'aggression', 'post_process_threshold'])
const BOOLEAN_KEYS = new Set(['standardize', 'normalize', 'enable_post_process', 'high_end_process'])
const VR_KEYS = new Set(['window_size', 'aggression', 'post_process_threshold', 'enable_post_process', 'high_end_process'])
const own = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key)
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)
const safe = (n: unknown): n is number => typeof n === 'number' && Number.isSafeInteger(n)

export function normalizeCustomParameterIntent(value: unknown): CustomParameterIntent {
  if (!record(value)) return {}
  const next: CustomParameterIntent = {}
  for (const key of ['chunkMilliseconds', 'overlapBasisPoints', 'batchSize'] as const) {
    const v = value[key]
    if (safe(v) && v > 0 && (key !== 'overlapBasisPoints' || v <= 9900)) next[key] = v
  }
  for (const key of ['standardize', 'normalize'] as const) if (typeof value[key] === 'boolean') next[key] = value[key]
  if (record(value.vr)) {
    const vr: NonNullable<CustomParameterIntent['vr']> = {}
    for (const key of VR_KEYS) {
      const v = value.vr[key]
      if ((BOOLEAN_KEYS.has(key) && typeof v === 'boolean') || (NUMBER_KEYS.has(key) && typeof v === 'number' && Number.isFinite(v) && v >= 0)) {
        vr[key as keyof typeof vr] = v
      }
    }
    if (Object.keys(vr).length) next.vr = vr
  }
  return next
}

export function normalizeResolutionMetadata(value: unknown): ParameterResolutionMetadata | undefined {
  if (!record(value) || value.version !== 1 || !record(value.defaults) || !record(value.capabilities) || typeof value.fingerprint !== 'string' || !value.fingerprint) return
  const defaults: NativeParameters = {}
  for (const [key, v] of Object.entries(value.defaults)) {
    if ((NUMBER_KEYS.has(key) && typeof v === 'number' && Number.isFinite(v)) || (BOOLEAN_KEYS.has(key) && typeof v === 'boolean')) defaults[key] = v
  }
  const capabilities: Record<string, ParameterCapability> = {}
  for (const [key, cap] of Object.entries(value.capabilities)) {
    if (!record(cap) || !['free', 'lattice', 'fixed', 'unsupported', 'unknown'].includes(String(cap.kind))) continue
    const normalized: ParameterCapability = { kind: cap.kind as ParameterCapability['kind'] }
    for (const k of ['min', 'max', 'step', 'offset'] as const) if (typeof cap[k] === 'number' && Number.isFinite(cap[k])) normalized[k] = cap[k]
    if (typeof cap.reason === 'string') normalized.reason = cap.reason
    capabilities[key] = normalized
  }
  const overlap = record(value.overlap) ? value.overlap : {}
  return {
    version: 1, defaults, capabilities, fingerprint: value.fingerprint,
    ...(record(value.defaultSources) ? { defaultSources: Object.fromEntries(Object.entries(value.defaultSources).filter(([, source]) => source === 'model-config' || source === 'runtime-default')) as ParameterResolutionMetadata['defaultSources'] } : {}),
    ...(safe(value.sampleRate) && value.sampleRate > 0 ? { sampleRate: value.sampleRate } : {}),
    ...(typeof value.sampleRateSource === 'string' ? { sampleRateSource: value.sampleRateSource } : {}),
    overlap: {
      kind: ['samples', 'count'].includes(String(overlap.kind)) ? overlap.kind as 'samples' | 'count' : 'unknown',
      ...(safe(overlap.count) && overlap.count > 0 ? { count: overlap.count } : {}),
    },
  }
}

/** Pick the nearest legal lattice point, including endpoints for out-of-range requests. */
export function nearestLegalChunk(target: number, cap: ParameterCapability): number | undefined {
  if (!safe(target) || target < 1) return
  const min = Math.max(1, cap.min ?? 1), max = cap.max ?? Number.MAX_SAFE_INTEGER
  const step = cap.kind === 'lattice' ? cap.step : cap.step ?? 1
  const offset = cap.offset ?? 0
  if (!safe(step) || step < 1 || !safe(offset) || !safe(min) || !safe(max)) return
  const kmin = Math.ceil((min - offset) / step), kmax = Math.floor((max - offset) / step)
  if (kmin > kmax) return
  const lo = Math.floor((target - offset) / step)
  return [...new Set([lo, lo + 1, kmin, kmax])]
    .filter(k => k >= kmin && k <= kmax)
    .map(k => offset + k * step)
    .filter(safe)
    .sort((a, b) => Math.abs(a - target) - Math.abs(b - target) || a - b)[0]
}

export function resolveParameters(
  metadata: ParameterResolutionMetadata | undefined,
  library: Record<string, unknown> | undefined,
  mode: ParameterMode,
  intent: CustomParameterIntent,
): ResolvedParameters {
  const result: ResolvedParameters = { parameters: {}, sources: {}, adjustments: {}, skipped: [], issues: [] }
  if (!metadata) {
    result.issues.push({ key: 'metadata', code: 'metadataUnavailable' })
    return result
  }
  const p = result.parameters, source = result.sources
  Object.assign(p, metadata.defaults)
  for (const key of Object.keys(p)) source[key] = metadata.defaultSources?.[key] || 'model-config'
  let count = metadata.overlap.kind === 'count' ? metadata.overlap.count : undefined
  const baseC = p.chunk_size, baseO = p.overlap_size
  for (const [key, value] of Object.entries(library || {})) {
    if (!own(p, key) || (typeof value !== typeof p[key])) continue
    if (typeof value === 'number' && (!Number.isFinite(value) || (['chunk_size', 'batch_size', 'window_size'].includes(key) && value <= 0))) continue
    p[key] = value as number | boolean
    source[key] = 'model-library'
  }
  const libCount = library?.num_overlap
  const libraryOverlap = typeof library?.overlap_size === 'number' && library.overlap_size >= 0
  if (safe(libCount) && libCount > 0 && !libraryOverlap) count = libCount
  else if (libraryOverlap) count = undefined
  const ratioOverlap = (c: number, oldC: number, oldO: number) => {
    const product = c * (oldC - oldO)
    return Number.isSafeInteger(product) ? c - Math.max(1, Math.floor(product / oldC)) : undefined
  }
  if (typeof p.chunk_size === 'number' && typeof p.overlap_size === 'number' && p.chunk_size !== baseC && !libraryOverlap) {
    const c = p.chunk_size
    const o = count ? c - Math.floor(c / count) : typeof baseC === 'number' && typeof baseO === 'number' ? ratioOverlap(c, baseC, baseO) : undefined
    if (o !== undefined) p.overlap_size = o
  } else if (safe(libCount) && libCount > 0 && count && typeof p.chunk_size === 'number') {
    p.overlap_size = p.chunk_size - Math.floor(p.chunk_size / count)
    source.overlap_size = 'model-library'
  }
  const modelC = p.chunk_size, modelO = p.overlap_size
  const editable = (key: string) => {
    const kind = metadata.capabilities[key]?.kind
    if (kind === 'free' || kind === 'lattice') return true
    result.skipped.push(key)
    return false
  }
  const assign = (key: string, value: number | boolean) => {
    if (!own(p, key)) { result.skipped.push(key); return }
    if (!editable(key)) return
    p[key] = value; source[key] = 'custom'
  }
  if (mode === 'custom') {
    if (intent.chunkMilliseconds !== undefined && editable('chunk_size')) {
      const sr = metadata.sampleRate
      const product = sr && intent.chunkMilliseconds * sr
      if (!sr) result.issues.push({ key: 'chunk_size', code: 'sampleRateMissing' })
      else if (!safe(product) || !safe(product + 500) || intent.chunkMilliseconds < 1) result.issues.push({ key: 'chunk_size', code: 'invalidValue' })
      else {
        const requested = Math.floor((product + 500) / 1000)
        const actual = nearestLegalChunk(requested, metadata.capabilities.chunk_size!)
        if (actual === undefined) result.issues.push({ key: 'chunk_size', code: 'noLegalChunk' })
        else {
          p.chunk_size = actual; source.chunk_size = 'custom'
          if (actual !== requested) result.adjustments.chunk_size = { requested, actual }
        }
      }
    }
    if (intent.batchSize !== undefined) assign('batch_size', intent.batchSize)
    for (const key of ['standardize', 'normalize'] as const) if (intent[key] !== undefined) assign(key, intent[key])
    for (const [key, value] of Object.entries(intent.vr || {})) if (value !== undefined) assign(key, value)
    if (typeof p.chunk_size === 'number' && typeof modelO === 'number' && typeof modelC === 'number') {
      if (intent.overlapBasisPoints !== undefined && editable('overlap_size')) {
        const bp = intent.overlapBasisPoints, product = p.chunk_size * (10000 - bp)
        if (!safe(bp) || bp < 1 || bp > 9900 || !safe(product)) result.issues.push({ key: 'overlap_size', code: 'invalidValue' })
        else { p.overlap_size = p.chunk_size - Math.max(1, Math.floor(product / 10000)); source.overlap_size = 'custom' }
      } else if (p.chunk_size !== modelC) {
        const o = count ? p.chunk_size - Math.floor(p.chunk_size / count) : ratioOverlap(p.chunk_size, modelC, modelO)
        if (o === undefined) result.issues.push({ key: 'overlap_size', code: 'invalidValue' })
        else p.overlap_size = o
      }
    } else if (intent.overlapBasisPoints !== undefined && own(p, 'overlap_size')) {
      result.issues.push({ key: 'overlap_size', code: 'invalidValue' })
    } else if (intent.overlapBasisPoints !== undefined) {
      result.skipped.push('overlap_size')
    }
  }
  for (const [key, value] of Object.entries(p)) {
    if (typeof value !== 'number') continue
    const cap = metadata.capabilities[key]
    if (!Number.isFinite(value) || (key !== 'post_process_threshold' && !safe(value)) || (cap?.min !== undefined && value < cap.min) || (cap?.max !== undefined && value > cap.max)) result.issues.push({ key, code: 'invalidValue' })
    if (key !== 'chunk_size' && cap?.kind === 'lattice' && cap.step && (value - (cap.offset || 0)) % cap.step !== 0) result.issues.push({ key, code: 'invalidValue' })
  }
  if (typeof p.overlap_size === 'number' && typeof p.chunk_size === 'number' && (p.overlap_size < 0 || p.overlap_size >= p.chunk_size)) result.issues.push({ key: 'overlap_size', code: 'invalidValue' })
  if (typeof p.chunk_size === 'number') {
    const cap = metadata.capabilities.chunk_size
    if (cap && ['free', 'lattice'].includes(cap.kind) && nearestLegalChunk(p.chunk_size, cap) !== p.chunk_size) result.issues.push({ key: 'chunk_size', code: 'noLegalChunk' })
  }
  delete p.num_overlap
  return result
}

export function createInferenceSnapshot(metadata: ParameterResolutionMetadata, resolved: ResolvedParameters, mode: ParameterMode, intent: CustomParameterIntent, useTta: boolean): ResolvedInferenceSnapshot {
  if (resolved.issues.length) throw new Error(resolved.issues[0]!.code)
  return JSON.parse(JSON.stringify({ version: 1, fingerprint: metadata.fingerprint, parameters: resolved.parameters, useTta, mode, intent, sources: resolved.sources, adjustments: resolved.adjustments }))
}

/** Persist only explicitly edited model fields, without copying unrelated defaults. */
export function applyLibraryIntent(metadata: ParameterResolutionMetadata, library: Record<string, unknown> | undefined, intent: CustomParameterIntent): NativeParameters {
  const resolved = resolveParameters(metadata, library, 'custom', intent)
  if (resolved.issues.length) throw new Error(resolved.issues[0]!.code)
  const next: NativeParameters = {}
  for (const [key, value] of Object.entries(library || {})) if ((NUMBER_KEYS.has(key) && typeof value === 'number') || (BOOLEAN_KEYS.has(key) && typeof value === 'boolean')) next[key] = value as number | boolean
  for (const [key, source] of Object.entries(resolved.sources)) if (source === 'custom') next[key] = resolved.parameters[key]!
  if (next.num_overlap !== undefined) {
    if (typeof resolved.parameters.overlap_size !== 'number') throw new Error('metadataUnavailable')
    next.overlap_size = resolved.parameters.overlap_size
    delete next.num_overlap
  }
  if (intent.chunkMilliseconds !== undefined && typeof resolved.parameters.overlap_size === 'number') next.overlap_size = resolved.parameters.overlap_size
  if (intent.chunkMilliseconds !== undefined || intent.overlapBasisPoints !== undefined) delete next.num_overlap
  for (const key of Object.keys(next)) {
    if (next[key] !== metadata.defaults[key]) continue
    if (key === 'overlap_size') {
      // The original overlap can still be an override when a different chunk changes its ratio.
      const { overlap_size: _overlap, ...withoutOverlap } = next
      if (resolveParameters(metadata, withoutOverlap, 'model', {}).parameters.overlap_size !== next.overlap_size) continue
    }
    delete next[key]
  }
  return next
}
