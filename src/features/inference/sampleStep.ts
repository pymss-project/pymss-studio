export type InferenceParamMeta = {
  recommendedSampleStep?: number
  source?: string
  chunkSizeConstraint?: { step: number; offset: number; min: number }
}

const MAX_INFERENCE_SAMPLE_VALUE = 1_048_576
const INFERENCE_SAMPLE_STEP_MULTIPLIER = 10

export function normalizeInferenceParamMeta(input: unknown): InferenceParamMeta | undefined {
  if (!input || typeof input !== 'object') return undefined
  const source = input as Record<string, unknown>
  const rawStep = Number(source.recommendedSampleStep)
  if (!Number.isFinite(rawStep) || rawStep < 1 || rawStep > MAX_INFERENCE_SAMPLE_VALUE) return undefined
  const recommendedSampleStep = Math.floor(rawStep)
  const rawSource = typeof source.source === 'string' ? source.source.trim() : ''
  const constraint = source.chunkSizeConstraint as Record<string, unknown> | undefined
  const step = Number(constraint?.step)
  const offset = Number(constraint?.offset)
  const min = Number(constraint?.min)
  const validConstraint = Number.isSafeInteger(step) && step > 0 && step <= MAX_INFERENCE_SAMPLE_VALUE
    && Number.isSafeInteger(offset) && offset >= 0 && offset < step
    && Number.isSafeInteger(min) && min > 0 && min <= MAX_INFERENCE_SAMPLE_VALUE
    && (min - offset) % step === 0
  return {
    recommendedSampleStep,
    ...(rawSource ? { source: rawSource } : {}),
    ...(validConstraint ? { chunkSizeConstraint: { step, offset, min } } : {}),
  }
}

export function resolveInferenceSampleStep(meta: InferenceParamMeta | undefined, fallback: number) {
  const normalized = normalizeInferenceParamMeta(meta)
  if (!normalized?.recommendedSampleStep) return fallback
  const scaled = normalized.recommendedSampleStep * INFERENCE_SAMPLE_STEP_MULTIPLIER
  return scaled <= MAX_INFERENCE_SAMPLE_VALUE ? scaled : normalized.recommendedSampleStep
}

export function resolveInferenceChunkStep(meta: InferenceParamMeta | undefined, fallback: number) {
  return normalizeInferenceParamMeta(meta)?.chunkSizeConstraint?.step || resolveInferenceSampleStep(meta, fallback)
}

export function inferenceChunkSizeIssue(value: number | null | undefined, meta: InferenceParamMeta | undefined) {
  const constraint = normalizeInferenceParamMeta(meta)?.chunkSizeConstraint
  if (!constraint || value == null || value === 0) return undefined
  if (Number.isSafeInteger(value) && value >= constraint.min && (value - constraint.offset) % constraint.step === 0) {
    return undefined
  }
  const candidate = Number.isFinite(value) ? value : constraint.min
  const lower = Math.max(constraint.min, constraint.offset + Math.floor((candidate - constraint.offset) / constraint.step) * constraint.step)
  const upper = Math.max(constraint.min, constraint.offset + Math.ceil((candidate - constraint.offset) / constraint.step) * constraint.step)
  const values = [...new Set([lower, upper])].filter(value => value <= MAX_INFERENCE_SAMPLE_VALUE)
  if (!values.length) {
    values.push(constraint.offset + Math.floor((MAX_INFERENCE_SAMPLE_VALUE - constraint.offset) / constraint.step) * constraint.step)
  }
  return { values: values.join(' / ') }
}

export function alignInferenceStepChange(
  current: number | null,
  next: number | null,
  alignmentStep: number | undefined,
  controlStep: number,
  alignmentOffset = 0,
  alignmentMin = 0,
) {
  if (
    typeof current !== 'number'
    || !Number.isFinite(current)
    || typeof next !== 'number'
    || !Number.isFinite(next)
    || typeof alignmentStep !== 'number'
    || !Number.isFinite(alignmentStep)
    || alignmentStep < 1
    || (current >= alignmentMin && (current - alignmentOffset) % alignmentStep === 0)
  ) {
    return next
  }
  const delta = next - current
  if (Math.abs(delta) !== controlStep) return next
  return Math.max(alignmentMin, alignmentOffset + (delta > 0
    ? Math.ceil((current - alignmentOffset) / alignmentStep)
    : Math.floor((current - alignmentOffset) / alignmentStep)) * alignmentStep)
}
