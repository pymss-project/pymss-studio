import type { CustomParameterIntent, ParameterResolutionMetadata } from './parameters'

export const INFERENCE_PRESETS = [
  { id: 'speed', label: 'parameterEditor.presets.speed', chunkMilliseconds: 8000, overlapBasisPoints: 2000 },
  { id: 'balanced', label: 'parameterEditor.presets.balanced', chunkMilliseconds: 10000, overlapBasisPoints: 5000 },
  { id: 'quality', label: 'parameterEditor.presets.quality', chunkMilliseconds: 14000, overlapBasisPoints: 8000 },
] as const

export type InferencePreset = typeof INFERENCE_PRESETS[number]

export function matchingInferencePreset(intent: CustomParameterIntent) {
  return INFERENCE_PRESETS.find(preset => preset.chunkMilliseconds === intent.chunkMilliseconds
    && preset.overlapBasisPoints === intent.overlapBasisPoints)?.id
}

export function supportsInferencePresets(metadata: ParameterResolutionMetadata | undefined) {
  return Boolean(metadata?.sampleRate && Number.isSafeInteger(metadata.sampleRate) && metadata.sampleRate > 0
    && typeof metadata.defaults.chunk_size === 'number' && typeof metadata.defaults.overlap_size === 'number'
    && ['chunk_size', 'overlap_size'].every(key => ['free', 'lattice'].includes(metadata.capabilities[key]?.kind || '')))
}
