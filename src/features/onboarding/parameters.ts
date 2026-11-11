import type { ParameterHelpTopic } from '../inference/parameterHelp'

export type ParameterGuideId = 'tta' | 'wavBitDepth' | 'flacBitDepth' | 'mp3BitRate' | 'm4aBitRate' | 'm4aCodec'
  | 'normalize' | 'batch_size' | 'overlap_size' | 'num_overlap' | 'chunk_size' | 'window_size' | 'aggression'
  | 'post_process_threshold' | 'standardize' | 'enable_post_process' | 'high_end_process' | 'modelDefaults'
export type ParameterGuide = {
  id: ParameterGuideId
  group: 'inference' | 'output'
  title: string
  description: string
  diagram?: ParameterHelpTopic
}

export const PARAMETER_GUIDES: readonly ParameterGuide[] = [
  { id: 'tta', group: 'inference', title: 'separate.tta', description: 'beginnerGuide.ttaFieldDescription', diagram: 'tta' },
  { id: 'batch_size', group: 'inference', title: 'inference.batchSize', description: 'parameterHelp.batchDetail', diagram: 'batch' },
  { id: 'overlap_size', group: 'inference', title: 'parameterEditor.overlapPercent', description: 'beginnerGuide.overlapFieldDescription', diagram: 'overlap' },
  { id: 'num_overlap', group: 'inference', title: 'inference.numOverlap', description: 'beginnerGuide.overlapCountFieldDescription', diagram: 'overlap' },
  { id: 'chunk_size', group: 'inference', title: 'parameterEditor.chunkSeconds', description: 'beginnerGuide.chunkFieldDescription', diagram: 'chunk' },
  { id: 'window_size', group: 'inference', title: 'inference.vrWindowSize', description: 'beginnerGuide.windowFieldDescription', diagram: 'vrWindow' },
  { id: 'aggression', group: 'inference', title: 'inference.vrAggression', description: 'beginnerGuide.aggressionFieldDescription', diagram: 'vrAggression' },
  { id: 'post_process_threshold', group: 'inference', title: 'inference.vrPostProcessThreshold', description: 'beginnerGuide.postThresholdFieldDescription' },
  { id: 'standardize', group: 'inference', title: 'inference.standardize', description: 'parameterHelp.standardizeDescription', diagram: 'standardize' },
  { id: 'enable_post_process', group: 'inference', title: 'inference.vrEnablePostProcess', description: 'beginnerGuide.postProcessFieldDescription' },
  { id: 'high_end_process', group: 'inference', title: 'inference.vrHighEndProcess', description: 'beginnerGuide.highEndFieldDescription' },
  { id: 'modelDefaults', group: 'inference', title: 'beginnerGuide.modelDefaultsTitle', description: 'beginnerGuide.modelDefaultsDescription' },
  { id: 'wavBitDepth', group: 'output', title: 'audio.wavBitDepth', description: 'beginnerGuide.wavFieldDescription', diagram: 'bitDepth' },
  { id: 'flacBitDepth', group: 'output', title: 'audio.flacBitDepth', description: 'beginnerGuide.flacFieldDescription', diagram: 'bitDepth' },
  { id: 'mp3BitRate', group: 'output', title: 'audio.mp3BitRate', description: 'beginnerGuide.mp3FieldDescription', diagram: 'bitRate' },
  { id: 'm4aBitRate', group: 'output', title: 'audio.m4aBitRate', description: 'beginnerGuide.m4aFieldDescription', diagram: 'bitRate' },
  { id: 'm4aCodec', group: 'output', title: 'audio.m4aCodec', description: 'beginnerGuide.codecFieldDescription' },
  { id: 'normalize', group: 'output', title: 'inference.normalize', description: 'parameterHelp.normalizeDescription', diagram: 'normalize' },
]

export function visibleParameterGuides(context: {
  modelType?: string | null
  hasModel: boolean
  defaults: Record<string, unknown>
  format: string
  canSaveDefaults: boolean
}): ParameterGuide[] {
  const type = String(context.modelType || '').trim().toLowerCase()
  const formatFields: Record<string, string[]> = { wav: ['wavBitDepth'], flac: ['flacBitDepth'], mp3: ['mp3BitRate'], m4a: ['m4aBitRate', 'm4aCodec'] }
  return PARAMETER_GUIDES.filter(item => {
    if (item.id === 'tta') return true
    if (['wavBitDepth', 'flacBitDepth', 'mp3BitRate', 'm4aBitRate', 'm4aCodec'].includes(item.id)) return formatFields[context.format]?.includes(item.id) === true
    if (item.id === 'normalize') return context.hasModel
    if (item.id === 'standardize') return context.hasModel && type !== 'vr'
    if (item.id === 'modelDefaults') return context.canSaveDefaults
    if (item.id === 'num_overlap') return false
    return Object.hasOwn(context.defaults, item.id)
  }).map(item => item.id === 'tta' && type === 'vr' ? { ...item, diagram: 'vrTta' as const } : item)
}
