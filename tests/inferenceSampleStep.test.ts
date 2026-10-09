import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import {
  alignInferenceStepChange,
  normalizeInferenceParamMeta,
  resolveInferenceSampleStep,
  resolveInferenceChunkStep,
  inferenceChunkSizeIssue,
} from '../src/features/inference/sampleStep.ts'

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
