import { BUILTIN_SPECS, NODE_SPECS, type NodeSpec } from './nodeSpecs'

export type NodeTranslator = (key: string, named?: Record<string, unknown>) => unknown

let currentTranslator: NodeTranslator | undefined

const CATEGORY_KEYS: Record<string, string> = {
  'pymss/audio': 'audio',
  'pymss/params': 'params',
  'pymss/separate': 'separate',
  'pymss/output': 'output',
  'pymss/audio_tools': 'audioTools',
  'pymss/plugins': 'plugins',
  'pymss/string': 'string',
  'pymss/builtin': 'builtin',
}

const FIELD_KEYS: Record<string, string> = {
  '-a': 'invertedAudio',
  STRING: 'stringOutput',
  a: 'audioA',
  aggression: 'aggression',
  audio: 'audio',
  audio1: 'audio1',
  audio2: 'audio2',
  audio_left: 'audioLeft',
  audio_name: 'audioName',
  audio_right: 'audioRight',
  audios: 'audios',
  batch_size: 'batchSize',
  bitrate: 'bitrate',
  channels: 'channels',
  chunk_size: 'chunkSize',
  debug: 'debug',
  delimiter: 'delimiter',
  device: 'device',
  device_ids: 'deviceIds',
  direction: 'direction',
  download_missing: 'downloadMissing',
  duration: 'duration',
  enable_post_process: 'enablePostProcess',
  enable_tta: 'enableTta',
  end: 'end',
  ensemble_type: 'ensembleType',
  f_string: 'formatString',
  filename: 'filename',
  filename_prefix: 'filenamePrefix',
  find: 'find',
  flac_bit_depth: 'flacBitDepth',
  folder: 'folder',
  format: 'outputFormat',
  group_index: 'groupIndex',
  high_end_process: 'highEndProcess',
  high_freq: 'highFrequency',
  high_gain_dB: 'highGain',
  input_count: 'inputCount',
  input_name: 'inputName',
  json_string: 'jsonString',
  key: 'key',
  left: 'leftChannel',
  low_freq: 'lowFrequency',
  low_gain_dB: 'lowGain',
  merge_method: 'mergeMethod',
  mid_freq: 'midFrequency',
  mid_gain_dB: 'midGain',
  mid_q: 'midQ',
  mode: 'mode',
  model_name: 'modelName',
  model_type: 'modelType',
  mp3_bit_rate: 'mp3BitRate',
  mss_params: 'mssParams',
  normalize: 'normalize',
  output_folder: 'outputFolder',
  output_format: 'outputFormat',
  overlap_size: 'overlapSize',
  params: 'params',
  post_process_threshold: 'postProcessThreshold',
  quality: 'quality',
  recursive: 'recursive',
  regex_pattern: 'regexPattern',
  replace: 'replace',
  right: 'rightChannel',
  sample_rate: 'sampleRate',
  sort_files: 'sortFiles',
  source: 'source',
  standardize: 'standardize',
  start: 'start',
  start_index: 'startIndex',
  stem_names: 'stemNames',
  string: 'string',
  string_a: 'stringA',
  string_b: 'stringB',
  value: 'value',
  volume: 'volume',
  vr_params: 'vrParams',
  wav_bit_depth: 'wavBitDepth',
  window_size: 'windowSize',
}

const MENU_KEYS: Record<string, string> = {
  'Add Group': 'addGroup',
  'Add Node': 'addNode',
  'Add Reroute': 'addReroute',
  'Align Selected To': 'alignSelected',
  Always: 'always',
  Clone: 'clone',
  Collapse: 'collapse',
  Colors: 'colors',
  'Convert to Subgraph 🆕': 'convertToSubgraph',
  'Cannot remove': 'cannotRemove',
  Delete: 'delete',
  'Delete Reroute': 'deleteReroute',
  'Disconnect Links': 'disconnectLinks',
  'Distribute Nodes': 'distributeNodes',
  'Edit Group': 'editGroup',
  Expand: 'expand',
  'Hide Advanced': 'hideAdvanced',
  Inputs: 'inputs',
  Mode: 'mode',
  Never: 'never',
  'On Event': 'onEvent',
  'On Executed': 'onExecuted',
  'On Trigger': 'onTrigger',
  Outputs: 'outputs',
  Pin: 'pin',
  Properties: 'properties',
  'Properties Panel': 'propertiesPanel',
  Remove: 'remove',
  'Remove Slot': 'removeSlot',
  'Rename Slot': 'renameSlot',
  Resize: 'resize',
  Search: 'search',
  Shapes: 'shapes',
  'Show Advanced': 'showAdvanced',
  Title: 'title',
  Unpin: 'unpin',
}

const ADVANCED_EDITOR_TEXT_KEYS = {
  searchNodes: 'workflows.advancedEditor.searchNodes',
  value: 'workflows.advancedEditor.value',
  confirm: 'workflows.advancedEditor.confirm',
} as const

function translate(
  key: string,
  fallback: string,
  named?: Record<string, unknown>,
  translator: NodeTranslator | undefined = currentTranslator,
) {
  if (!translator) return fallback
  try {
    const value = translator(key, named)
    return typeof value === 'string' && value !== key ? value : fallback
  } catch {
    return fallback
  }
}

export function setNodeTranslator(translator?: NodeTranslator) {
  currentTranslator = translator
}

export function translateNodeTitle(spec: NodeSpec, translator?: NodeTranslator) {
  return translate(
    `workflows.advancedEditor.nodes.${spec.type}`,
    spec.title,
    undefined,
    translator || currentTranslator,
  )
}

export function translateNodeCategory(category: string, translator?: NodeTranslator) {
  const categoryKey = CATEGORY_KEYS[category]
  if (!categoryKey) return category
  return `pymss/${translate(
    `workflows.advancedEditor.categories.${categoryKey}`,
    category.slice(category.indexOf('/') + 1),
    undefined,
    translator || currentTranslator,
  )}`
}

export function translateNodeField(name: string, translator?: NodeTranslator) {
  const indexedAudio = /^audio_(\d+)$/.exec(name)
  if (indexedAudio) {
    return translate(
      'workflows.advancedEditor.fields.audioIndexed',
      name,
      { index: indexedAudio[1] },
      translator || currentTranslator,
    )
  }

  const indexedWeight = /^weight_(\d+)$/.exec(name)
  if (indexedWeight) {
    return translate(
      'workflows.advancedEditor.fields.weightIndexed',
      name,
      { index: indexedWeight[1] },
      translator || currentTranslator,
    )
  }

  const typedStem = /^(.*) \((Audio|String)\)$/.exec(name)
  if (typedStem) {
    const suffix = translate(
      typedStem[2] === 'Audio'
        ? 'workflows.advancedEditor.fields.audioType'
        : 'workflows.advancedEditor.fields.stringType',
      typedStem[2],
      undefined,
      translator || currentTranslator,
    )
    return `${typedStem[1]} (${suffix})`
  }

  const fieldKey = FIELD_KEYS[name]
  if (!fieldKey) return name
  return translate(
    `workflows.advancedEditor.fields.${fieldKey}`,
    name,
    undefined,
    translator || currentTranslator,
  )
}

export function translateLiteGraphMenuText(value: string, translator?: NodeTranslator) {
  const menuKey = MENU_KEYS[value]
  if (menuKey) {
    return translate(
      `workflows.advancedEditor.menu.${menuKey}`,
      value,
      undefined,
      translator || currentTranslator,
    )
  }
  const spec = NODE_SPECS[value]
    || BUILTIN_SPECS[value]
    || NODE_SPECS[value.replace(/^pymss_/, '')]
  return spec ? translateNodeTitle(spec, translator) : value
}

export function translateAdvancedEditorText(
  key: 'searchNodes' | 'value' | 'confirm',
  fallback: string,
  translator?: NodeTranslator,
) {
  return translate(
    ADVANCED_EDITOR_TEXT_KEYS[key],
    fallback,
    undefined,
    translator || currentTranslator,
  )
}
