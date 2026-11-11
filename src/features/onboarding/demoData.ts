import type { ModelEntry } from '@/stores/model'
import type { EditorSource, EditorTrack } from '@/types/editor'

export const DEMO_INFERENCE_DEFAULTS = { batch_size: 1, overlap_size: 97020, chunk_size: 485100, standardize: true, normalize: false }

export function createDemoModels(): ModelEntry[] {
  return ['BS-Roformer Vocals', 'MelBand-Roformer Vocals', 'MDX Instrumental'].map((name, index) => ({
    name, aliases: [], modelType: index === 2 ? 'mdx' : 'bs_roformer', architecture: index === 2 ? 'MDX' : 'Roformer',
    supported: true, unsupportedReason: '', category: 'MSS', categoryCn: 'MSS', primaryCategory: 'MSS', primaryCategoryCn: 'MSS',
    secondaryCategory: '', secondaryCategoryCn: '', targetStem: index === 2 ? 'Instrumental' : 'Vocals',
    configInstruments: 'Vocals/Instrumental', configTargetInstrument: index === 2 ? 'Instrumental' : 'Vocals',
    classificationConfidence: '', classificationBasis: '', sizeBytes: (index + 1) * 104857600, sha256: '', downloaded: false,
    missingPaths: [], modelPath: '', configPath: null, auxiliaryPaths: [], source: 'catalog',
    defaultInferenceParams: { ...DEMO_INFERENCE_DEFAULTS }, defaultInferenceParamsResolved: true, defaultInferenceParamsSource: 'config',
    inferenceParamMeta: { resolution: {
      version: 1, sampleRate: 44100, sampleRateSource: 'tutorial', fingerprint: `tutorial-${index}`,
      defaults: { ...DEMO_INFERENCE_DEFAULTS }, overlap: { kind: 'samples' },
      capabilities: {
        batch_size: { kind: 'free', min: 1, step: 1 }, chunk_size: { kind: 'free', min: 1025, step: 1 },
        overlap_size: { kind: 'free', min: 0, step: 1 }, standardize: { kind: 'free' }, normalize: { kind: 'free' },
      },
    } },
  }))
}

export function createDemoWorkflow(model: string): Record<string, unknown> {
  return { version: 1, defaults: { device: 'auto', output_format: 'wav' }, steps: [{
    id: 'vocals', model, input: 'input', stems: ['Vocals', 'Instrumental'],
    save: { Vocals: 'Default', Instrumental: 'Default' },
  }] }
}

export function createDemoAudio(): { sources: EditorSource[]; tracks: EditorTrack[] } {
  const sources: EditorSource[] = ['Vocals', 'Instrumental'].map((name, index) => ({
    id: 'source-' + index, role: 'stem', stemKey: name, path: '', name, duration: 12, sampleRate: 44100, channels: 2,
    peaks: Array.from({ length: 480 }, (_, sample) => Math.min(1,
      Math.abs(Math.sin(sample * 0.13 + index) * Math.cos(sample * 0.037)) * 0.7 + Math.abs(Math.sin(sample * 1.71)) * 0.15)),
  }))
  const tracks: EditorTrack[] = sources.map((source, index) => ({
    id: 'track-' + index, sourceId: source.id, role: 'stem', name: source.name, volume: 1, pan: 0,
    muted: false, solo: false, fadeIn: 0, fadeOut: 0, color: index ? '#88b8ad' : '#8daee2',
    clips: [{ id: 'clip-' + index, assetId: source.id, start: 0, offset: 0, duration: 12, volume: 1, fadeIn: 0, fadeOut: 0, muted: false, locked: false }],
  }))
  return { sources, tracks }
}
