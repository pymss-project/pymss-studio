import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import {
  alignInferenceStepChange,
  normalizeInferenceParamMeta,
  resolveInferenceSampleStep,
  resolveInferenceChunkStep,
  inferenceChunkSizeIssue,
} from '../src/features/inference/sampleStep.ts'
import { applyLibraryIntent, createInferenceSnapshot, nearestLegalChunk, normalizeCustomParameterIntent, resolveParameters, type ParameterResolutionMetadata } from '../src/features/inference/parameters.ts'

describe('inference sample-step metadata', () => {
  const mdx = { recommendedSampleStep: 1024, chunkSizeConstraint: { step: 32768, offset: 31744, min: 31744 } }

  it('preserves MDX chunk constraints separately from overlap steps', () => {
    assert.deepEqual(normalizeInferenceParamMeta(mdx), mdx)
    assert.equal(resolveInferenceChunkStep(mdx, 1024), 32768)
    assert.equal(resolveInferenceSampleStep(mdx, 1), 10240)
    assert.equal(resolveInferenceChunkStep(undefined, 1024), 1024)
    assert.equal(normalizeInferenceParamMeta({ ...mdx, chunkSizeConstraint: { step: 0, offset: 0, min: 0 } })?.chunkSizeConstraint, undefined)
  })

  it('detects invalid manual chunks without changing them or default-value sentinels', () => {
    assert.deepEqual(inferenceChunkSizeIssue(465920, mdx), { values: '457728 / 490496' })
    for (const value of [0, null, undefined, 261120, 457728, 490496]) assert.equal(inferenceChunkSizeIssue(value, mdx), undefined)
    assert.deepEqual(inferenceChunkSizeIssue(1000, mdx), { values: '31744' })
    assert.deepEqual(inferenceChunkSizeIssue(1_048_576, mdx), { values: '1047552' })
    assert.deepEqual(inferenceChunkSizeIssue(1_100_000, mdx), { values: '1047552' })
    assert.deepEqual(inferenceChunkSizeIssue(Number.MAX_VALUE, mdx), { values: '1047552' })
    assert.deepEqual(inferenceChunkSizeIssue(1_100_000, {
      recommendedSampleStep: 512,
      chunkSizeConstraint: { step: 8192, offset: 7680, min: 7680 },
    }), { values: '1048064' })
    assert.equal(inferenceChunkSizeIssue(465920, { recommendedSampleStep: 1024 }), undefined)
  })

  it('moves MDX step controls along the offset grid and recovers invalid stored values', () => {
    assert.equal(alignInferenceStepChange(261120, 293888, 32768, 32768, 31744, 31744), 293888)
    assert.equal(alignInferenceStepChange(465920, 498688, 32768, 32768, 31744, 31744), 490496)
    assert.equal(alignInferenceStepChange(465920, 433152, 32768, 32768, 31744, 31744), 457728)
    assert.equal(alignInferenceStepChange(0, 32768, 32768, 32768, 31744, 31744), 31744)
    assert.equal(alignInferenceStepChange(465920, 465921, 32768, 32768, 31744, 31744), 465921)
  })
  it('normalizes positive integer steps and trims their source', () => {
    assert.deepEqual(
      normalizeInferenceParamMeta({ recommendedSampleStep: 512.9, source: ' model.stft_hop_length ' }),
      { recommendedSampleStep: 512, source: 'model.stft_hop_length' },
    )
  })

  it('rejects missing and non-positive steps', () => {
    assert.equal(normalizeInferenceParamMeta(undefined), undefined)
    assert.equal(normalizeInferenceParamMeta({ recommendedSampleStep: 0 }), undefined)
    assert.equal(normalizeInferenceParamMeta({ recommendedSampleStep: Number.NaN }), undefined)
    assert.equal(normalizeInferenceParamMeta({ recommendedSampleStep: 1_048_577 }), undefined)
  })

  it('uses the existing control fallback when metadata is unavailable', () => {
    assert.equal(resolveInferenceSampleStep({ recommendedSampleStep: 441 }, 1024), 4410)
    assert.equal(resolveInferenceSampleStep({ recommendedSampleStep: 512 }, 1024), 5120)
    assert.equal(resolveInferenceSampleStep(undefined, 1024), 1024)
    assert.equal(resolveInferenceSampleStep({ recommendedSampleStep: -1 }, 1), 1)
  })

  it('aligns the first step-button click in the requested direction', () => {
    assert.equal(alignInferenceStepChange(24_000, 28_410, 441, 4_410), 24_255)
    assert.equal(alignInferenceStepChange(24_000, 19_590, 441, 4_410), 23_814)
    assert.equal(alignInferenceStepChange(24_255, 28_665, 441, 4_410), 28_665)
    assert.equal(alignInferenceStepChange(24_000, 25_000, 441, 4_410), 25_000)
  })
})

function parameterMeta(rate = 44100, chunk = 573300): ParameterResolutionMetadata {
  return {
    version: 1, sampleRate: rate, fingerprint: 'config-runtime-model-fingerprint',
    defaults: { batch_size: 1, chunk_size: chunk, overlap_size: Math.floor(chunk / 20), standardize: false, normalize: false },
    overlap: { kind: 'samples' },
    capabilities: { batch_size: { kind: 'free', min: 1, max: 32 }, chunk_size: { kind: 'free', min: 1025 }, overlap_size: { kind: 'free', min: 0 }, standardize: { kind: 'free' }, normalize: { kind: 'free' } },
  }
}

describe('explicit inference parameter sources', () => {
  it('keeps shared time and ratio intent across models, but leaves unmodified fields inherited', () => {
    const intent = { chunkMilliseconds: 10000, overlapBasisPoints: 5000 }
    const a = resolveParameters(parameterMeta(), { batch_size: 3 }, 'custom', intent)
    const b = resolveParameters(parameterMeta(48000, 624000), { batch_size: 7 }, 'custom', intent)
    assert.deepEqual([a.parameters.chunk_size, a.parameters.overlap_size, a.parameters.batch_size], [441000, 220500, 3])
    assert.deepEqual([b.parameters.chunk_size, b.parameters.overlap_size, b.parameters.batch_size], [480000, 240000, 7])
    assert.deepEqual(intent, { chunkMilliseconds: 10000, overlapBasisPoints: 5000 })
    assert.equal(a.sources.chunk_size, 'custom')
    assert.equal(a.sources.batch_size, 'model-library')
    const following = resolveParameters(parameterMeta(), { batch_size: 3 }, 'model', intent)
    assert.equal(following.parameters.chunk_size, 573300)
    assert.equal(resolveParameters(parameterMeta(), {}, 'custom', intent).parameters.chunk_size, 441000)
  })

  it('snaps to real MDX grid points including endpoints and lower ties without rewriting intent', () => {
    const cap = { kind: 'lattice', step: 32768, offset: 31744, min: 31744, max: 490496 } as const
    assert.equal(nearestLegalChunk(441000, cap), 424960)
    assert.equal(nearestLegalChunk(480000, cap), 490496)
    assert.equal(nearestLegalChunk(48128, cap), 31744)
    assert.equal(nearestLegalChunk(1400000, cap), 490496)
    assert.equal(nearestLegalChunk(1000, cap), 31744)
    assert.equal(nearestLegalChunk(1000, { ...cap, max: 20000 }), undefined)
    const metadata = parameterMeta()
    metadata.capabilities.chunk_size = cap
    metadata.defaults.chunk_size = 261120
    metadata.defaults.overlap_size = 130560
    const intent = { chunkMilliseconds: 10000 }
    const resolved = resolveParameters(metadata, {}, 'custom', intent)
    assert.equal(resolved.parameters.chunk_size, 424960)
    assert.deepEqual(resolved.adjustments.chunk_size, { requested: 441000, actual: 424960 })
    assert.equal(intent.chunkMilliseconds, 10000)
  })

  it('preserves exact inherited overlap and count semantics, including genuine zero overlap', () => {
    const meta = parameterMeta()
    meta.defaults.overlap_size = 12345
    assert.equal(resolveParameters(meta, {}, 'custom', {}).parameters.overlap_size, 12345)
    meta.defaults.overlap_size = 0
    assert.equal(resolveParameters(meta, {}, 'custom', { chunkMilliseconds: 10000 }).parameters.overlap_size, 0)
    meta.overlap = { kind: 'count', count: 4 }
    const result = resolveParameters(meta, {}, 'custom', { chunkMilliseconds: 10010 })
    const c = result.parameters.chunk_size as number
    assert.equal(result.parameters.overlap_size, c - Math.floor(c / 4))
    assert.equal('num_overlap' in result.parameters, false)
    const zeroLibrary = resolveParameters(parameterMeta(), { overlap_size: 0, num_overlap: 4 }, 'model', {})
    assert.equal(zeroLibrary.parameters.overlap_size, 0)
  })

  it('keeps false and VR zero as explicit values, while unavailable custom fields are visible and inactive', () => {
    const meta = parameterMeta()
    meta.defaults = { batch_size: 2, window_size: 512, aggression: 5, post_process_threshold: 0.2, normalize: true }
    meta.capabilities = { batch_size: { kind: 'free', min: 1 }, window_size: { kind: 'free', min: 1 }, aggression: { kind: 'free', min: 0, max: 100 }, post_process_threshold: { kind: 'free', min: 0, max: 1 }, normalize: { kind: 'free' } }
    const intent = { chunkMilliseconds: 10000, overlapBasisPoints: 5000, normalize: false, vr: { aggression: 0, post_process_threshold: 0 } }
    const result = resolveParameters(meta, {}, 'custom', intent)
    assert.equal(result.parameters.normalize, false)
    assert.equal(result.parameters.aggression, 0)
    assert.equal(result.parameters.post_process_threshold, 0)
    assert.deepEqual(result.skipped.sort(), ['chunk_size', 'overlap_size'])
    assert.deepEqual(normalizeCustomParameterIntent(intent), intent)
    meta.capabilities.window_size = { kind: 'lattice', min: 272, step: 16, offset: 0 }
    assert.equal(resolveParameters(meta, {}, 'custom', { vr: { window_size: 300 } }).issues[0]?.key, 'window_size')
    assert.deepEqual(resolveParameters(meta, {}, 'custom', { vr: { window_size: 320 } }).issues, [])
  })

  it('distinguishes missing sample rate from unsupported duration and validates an explicit request', () => {
    const meta = parameterMeta()
    delete meta.sampleRate
    assert.deepEqual(resolveParameters(meta, {}, 'model', {}).issues, [])
    assert.equal(resolveParameters(meta, {}, 'custom', { chunkMilliseconds: 10000 }).issues[0]?.code, 'sampleRateMissing')
    assert.equal(resolveParameters(meta, {}, 'custom', { overlapBasisPoints: 5000 }).parameters.overlap_size, 286650)
    assert.equal(resolveParameters(undefined, {}, 'model', {}).issues[0]?.code, 'metadataUnavailable')
    assert.equal(resolveParameters(parameterMeta(48000), {}, 'custom', { chunkMilliseconds: 30000 }).parameters.chunk_size, 1440000)
  })

  it('saves only edited library fields and snapshots independent deep copies', () => {
    const meta = parameterMeta()
    const intent = { overlapBasisPoints: 5000 }
    const profile = applyLibraryIntent(meta, { batch_size: 3 }, intent)
    assert.deepEqual(profile, { batch_size: 3, overlap_size: 286650 })
    assert.deepEqual(applyLibraryIntent(meta, { num_overlap: 4 }, { batchSize: 2 }), { batch_size: 2, overlap_size: 429975 })
    const resolved = resolveParameters(meta, profile, 'custom', intent)
    const snapshot = createInferenceSnapshot(meta, resolved, 'custom', intent, true)
    intent.overlapBasisPoints = 7500
    profile.batch_size = 8
    resolved.parameters.overlap_size = 7
    assert.equal(snapshot.parameters.overlap_size, 286650)
    assert.equal(snapshot.parameters.batch_size, 3)
    assert.equal(snapshot.intent.overlapBasisPoints, 5000)
    assert.equal(snapshot.useTta, true)
  })

  it('preserves an overlap equal to the original default when the saved chunk changes its basis', () => {
    const meta = parameterMeta(44100, 441000)
    meta.defaults.overlap_size = 220500
    const intent = { chunkMilliseconds: 20000, overlapBasisPoints: 2500 }
    const requested = resolveParameters(meta, undefined, 'custom', intent)
    assert.equal(requested.parameters.overlap_size, 220500)
    const profile = applyLibraryIntent(meta, undefined, intent)
    assert.equal(profile.overlap_size, 220500)
    assert.deepEqual(resolveParameters(meta, profile, 'model', {}).parameters, requested.parameters)

    // Editing another field later must retain the saved chunk/overlap pair as well.
    const next = applyLibraryIntent(meta, { chunk_size: 882000, overlap_size: 220500 }, { batchSize: 2 })
    assert.equal(resolveParameters(meta, next, 'model', {}).parameters.overlap_size, 220500)
  })

  it('prunes default overlap only when inheriting it preserves the resolved value', () => {
    const meta = parameterMeta(44100, 441000)
    meta.defaults.overlap_size = 220500
    assert.deepEqual(applyLibraryIntent(meta, undefined, { chunkMilliseconds: 10000, overlapBasisPoints: 5000 }), {})
    meta.defaults.overlap_size = 0
    assert.deepEqual(applyLibraryIntent(meta, undefined, { chunkMilliseconds: 20000 }), { chunk_size: 882000 })
  })
})
