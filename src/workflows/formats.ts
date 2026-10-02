export type WorkflowFormat = 'simple' | 'graph' | 'unknown'

export const WORKFLOW_FORMAT_VERSION = 1

export const SIMPLE_ENSEMBLE_ALGORITHMS = [
  'avg_wave', 'median_wave', 'min_wave', 'max_wave',
  'avg_fft', 'median_fft', 'min_fft', 'max_fft',
] as const
export const SIMPLE_AUDIO_OPERATIONS = ['sum', 'subtract', 'invert'] as const
export const SIMPLE_INFERENCE_NUMBER_FIELDS: ReadonlySet<string> = new Set([
  'batch_size', 'overlap_size', 'chunk_size', 'window_size', 'aggression', 'post_process_threshold',
])

export type SimpleEnsembleAlgorithm = typeof SIMPLE_ENSEMBLE_ALGORITHMS[number]
export type SimpleAudioOperation = typeof SIMPLE_AUDIO_OPERATIONS[number]
export type SimpleProcessingAlgorithm = SimpleEnsembleAlgorithm | SimpleAudioOperation

export function isSimpleAudioOperation(value: unknown): value is SimpleAudioOperation {
  return typeof value === 'string' && SIMPLE_AUDIO_OPERATIONS.includes(value as SimpleAudioOperation)
}

export function isSimpleProcessingAlgorithm(value: unknown): value is SimpleProcessingAlgorithm {
  return isSimpleAudioOperation(value)
    || (typeof value === 'string' && SIMPLE_ENSEMBLE_ALGORITHMS.includes(value as SimpleEnsembleAlgorithm))
}

export function simpleProcessingInputLimits(algorithm: SimpleProcessingAlgorithm) {
  if (algorithm === 'invert') return { min: 1, max: 1 }
  if (algorithm === 'subtract') return { min: 2, max: 2 }
  return { min: 2, max: 10 }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

export function hasInvalidSimpleInferenceNumbers(params: Record<string, unknown>): boolean {
  return Object.entries(params).some(([key, value]) => {
    if (!SIMPLE_INFERENCE_NUMBER_FIELDS.has(key)) return false
    if (value === null) return false
    if ((key === 'overlap_size' || key === 'chunk_size')
      && typeof value === 'string' && value.trim().toLowerCase() === 'default') return false
    if (typeof value !== 'number' || !Number.isFinite(value)) return true
    if (key === 'post_process_threshold') return value < 0 || value > 1
    const minimum = key === 'batch_size' || key === 'window_size' ? 1 : 0
    return !Number.isInteger(value) || value < minimum
  })
}

/**
 * Detect the editor format from the persisted definition. Existing workflow
 * records predate explicit format metadata, so the definition remains the
 * authoritative migration source.
 */
export function detectWorkflowFormat(definition: unknown): WorkflowFormat {
  if (!isRecord(definition)) return 'unknown'
  const hasSteps = Array.isArray(definition.steps)
  // pymss accepts graph definitions without a `links` field (or with null)
  // and treats them as graphs without connections.
  const hasNodes = Array.isArray(definition.nodes)
    && (definition.links == null || Array.isArray(definition.links))
  if (hasSteps === hasNodes) return 'unknown'
  return hasSteps ? 'simple' : 'graph'
}

export function isSimpleWorkflowDefinition(definition: unknown): definition is Record<string, unknown> {
  return detectWorkflowFormat(definition) === 'simple'
}

export function isGraphWorkflowDefinition(definition: unknown): definition is Record<string, unknown> {
  return detectWorkflowFormat(definition) === 'graph'
}

/**
 * Validate the structural parts shared by the simple editor and runtime.
 * Format detection guarantees a `steps` array, while this check keeps malformed
 * imported values from reaching hydration or the worker with a misleading
 * "no outputs" error (for example `defaults: "cuda"`).
 */
export function hasInvalidSimpleStructure(definition: Record<string, unknown>): boolean {
  if (!Array.isArray(definition.steps)) return true
  if (definition.ensembles != null && !Array.isArray(definition.ensembles)) return true
  if (definition.studio != null && !isRecord(definition.studio)) return true
  if (isRecord(definition.studio)) {
    if (definition.studio.editor !== 'simple') return true
    if (definition.studio.viewport != null && !isRecord(definition.studio.viewport)) return true
    if (definition.studio.nodes != null && !isRecord(definition.studio.nodes)) return true
  }
  if (definition.defaults != null && !isRecord(definition.defaults)) return true
  const defaults = isRecord(definition.defaults) ? definition.defaults : {}
  if (defaults.inference_params != null && !isRecord(defaults.inference_params)) return true
  if (isRecord(defaults.inference_params) && hasInvalidSimpleInferenceNumbers(defaults.inference_params)) return true
  const hasInvalidStep = (definition.steps as unknown[]).some((value) => {
    if (!isRecord(value)) return true
    if (value.inference_params != null && !isRecord(value.inference_params)) return true
    if (isRecord(value.inference_params) && hasInvalidSimpleInferenceNumbers(value.inference_params)) return true
    if (value.save != null && !isRecord(value.save)) return true
    return false
  })
  if (hasInvalidStep) return true
  const ensembles = Array.isArray(definition.ensembles) ? definition.ensembles : []
  const hasInvalidEnsemble = ensembles.some((value) => {
    if (!isRecord(value) || !Array.isArray(value.inputs)) return true
    if (typeof value.id !== 'string' || !value.id.trim()) return true
    if (!isSimpleProcessingAlgorithm(value.algorithm)) return true
    if (typeof value.output_stem !== 'string' || !value.output_stem.trim()) return true
    if (value.save != null && value.save !== false && typeof value.save !== 'string') return true
    if (value.output_name != null && typeof value.output_name !== 'string') return true
    const limits = simpleProcessingInputLimits(value.algorithm)
    const audioOperation = isSimpleAudioOperation(value.algorithm)
    if (value.inputs.length < limits.min || value.inputs.length > limits.max) return true
    return value.inputs.some((input) => (
      !isRecord(input)
      || typeof input.source !== 'string'
      || !input.source.trim()
      || typeof input.weight !== 'number'
      || !Number.isFinite(input.weight)
      || input.weight <= 0
      || (audioOperation && input.weight !== 1)
    ))
  })
  if (hasInvalidEnsemble) return true
  const stepOutputIndexes = new Map<string, number>()
  ;(definition.steps as unknown[]).forEach((value, index) => {
    if (!isRecord(value) || typeof value.id !== 'string' || !Array.isArray(value.stems)) return
    const stepId = value.id.trim()
    value.stems
      .filter(stem => typeof stem === 'string' && stem.trim())
      .forEach(stem => stepOutputIndexes.set(`${stepId}.${String(stem).trim()}`.toLowerCase(), index))
  })
  const availableEnsembleInputs = new Set(['input', ...stepOutputIndexes.keys()])
  for (const value of ensembles) {
    if (!isRecord(value) || !Array.isArray(value.inputs)) return true
    const sources = value.inputs.map(input => isRecord(input) && typeof input.source === 'string'
      ? input.source.trim().toLowerCase()
      : '')
    if (sources.some(source => !availableEnsembleInputs.has(source)) || new Set(sources).size !== sources.length) return true
    availableEnsembleInputs.add(`${String(value.id).trim()}.${String(value.output_stem).trim()}`.toLowerCase())
  }
  const ensembleOutputs = new Map(ensembles.flatMap(value => (
    isRecord(value) && typeof value.id === 'string' && typeof value.output_stem === 'string'
      ? [[`${value.id.trim()}.${value.output_stem.trim()}`.toLowerCase(), value] as const]
      : []
  )))
  function collectEnsembleStepDependencies(
    ensemble: Record<string, unknown>,
    visiting = new Set<string>(),
  ): number[] | null {
    const ensembleId = String(ensemble.id || '').trim().toLowerCase()
    if (!ensembleId || visiting.has(ensembleId) || !Array.isArray(ensemble.inputs)) return null
    const nextVisiting = new Set(visiting).add(ensembleId)
    const dependencies: number[] = []
    for (const input of ensemble.inputs) {
      if (!isRecord(input) || typeof input.source !== 'string') return null
      const source = input.source.trim().toLowerCase()
      if (source === 'input') continue
      const stepIndex = stepOutputIndexes.get(source)
      if (stepIndex !== undefined) {
        dependencies.push(stepIndex)
        continue
      }
      const nested = ensembleOutputs.get(source)
      if (!nested) return null
      const nestedDependencies = collectEnsembleStepDependencies(nested, nextVisiting)
      if (!nestedDependencies) return null
      dependencies.push(...nestedDependencies)
    }
    return dependencies
  }
  if ((definition.steps as unknown[]).some((value, targetIndex) => {
    if (!isRecord(value)) return true
    if (value.input != null && typeof value.input !== 'string') return true
    const input = typeof value.input === 'string' ? value.input.trim().toLowerCase() : 'input'
    if (input === 'input') return false
    const sourceIndex = stepOutputIndexes.get(input)
    if (sourceIndex !== undefined) return sourceIndex >= targetIndex
    const ensemble = ensembleOutputs.get(input)
    if (!ensemble) return true
    const dependencies = collectEnsembleStepDependencies(ensemble)
    return !dependencies || dependencies.some(dependencyIndex => dependencyIndex >= targetIndex)
  })) return true

  const outputNodeKeys = new Map<string, string>()
  ;(definition.steps as unknown[]).forEach((value) => {
    if (!isRecord(value) || typeof value.id !== 'string' || !Array.isArray(value.stems)) return
    const stepId = value.id.trim()
    value.stems.forEach((stem) => {
      if (typeof stem === 'string' && stem.trim()) {
        outputNodeKeys.set(`${stepId}.${stem.trim()}`.toLowerCase(), `step:${stepId.toLowerCase()}`)
      }
    })
  })
  ensembles.forEach((value) => {
    if (!isRecord(value)) return
    const output = `${String(value.id || '').trim()}.${String(value.output_stem || '').trim()}`.toLowerCase()
    outputNodeKeys.set(output, `ensemble:${String(value.id || '').trim().toLowerCase()}`)
  })
  const edges = new Map<string, Set<string>>()
  const addEdge = (source: string, target: string) => {
    if (!source || source === 'input') return
    const sourceNode = outputNodeKeys.get(source)
    if (!sourceNode) return
    const targets = edges.get(sourceNode) || new Set<string>()
    targets.add(target)
    edges.set(sourceNode, targets)
  }
  ;(definition.steps as unknown[]).forEach((value) => {
    if (!isRecord(value) || typeof value.id !== 'string') return
    const input = typeof value.input === 'string' ? value.input.trim().toLowerCase() : 'input'
    addEdge(input, `step:${value.id.trim().toLowerCase()}`)
  })
  ensembles.forEach((value) => {
    if (!isRecord(value) || !Array.isArray(value.inputs)) return
    value.inputs.forEach((input) => {
      if (isRecord(input) && typeof input.source === 'string') {
        addEdge(input.source.trim().toLowerCase(), `ensemble:${String(value.id).trim().toLowerCase()}`)
      }
    })
  })
  const visitingNodes = new Set<string>()
  const visitedNodes = new Set<string>()
  const hasCycleFrom = (node: string): boolean => {
    if (visitingNodes.has(node)) return true
    if (visitedNodes.has(node)) return false
    visitingNodes.add(node)
    if ([...(edges.get(node) || [])].some(hasCycleFrom)) return true
    visitingNodes.delete(node)
    visitedNodes.add(node)
    return false
  }
  if ([...outputNodeKeys.values()].some(hasCycleFrom)) return true
  const ids = [
    ...(definition.steps as unknown[]).flatMap(value => isRecord(value) && typeof value.id === 'string' ? [value.id.trim()] : []),
    ...ensembles.flatMap(value => isRecord(value) && typeof value.id === 'string' ? [value.id.trim()] : []),
  ].filter(Boolean)
  if (ids.some(id => id.includes('.'))) return true
  const normalizedIds = ids.map(id => id.toLowerCase())
  if (new Set(normalizedIds).size !== normalizedIds.length) return true
  // The pymss YAML parser accepts exactly version 1. Rejecting unsupported
  // versions here prevents the simple editor from rewriting a newer schema
  // as version 1 on save and gives the run screen a deterministic error.
  return (definition.steps.length > 0 || ensembles.length > 0) && definition.version !== WORKFLOW_FORMAT_VERSION
}

export function isWorkflowSeparationNodeType(value: unknown): boolean {
  const type = String(value || '').toLowerCase()
  return type.endsWith('_separate') || type.endsWith('_separate_list')
}

const WORKFLOW_SAVE_NODE_TYPES = new Set([
  'pymss_save_audio',
  'SaveAudio',
  'SaveAudioMP3',
  'SaveAudioOpus',
  'SaveAudioAdvanced',
])

export function isWorkflowSaveNodeType(value: unknown): boolean {
  return WORKFLOW_SAVE_NODE_TYPES.has(String(value || ''))
}

const LEGACY_SIMPLE_SAVE_FILENAME = /\.(?:wav|flac|mp3|m4a)$/iu
const DEFAULT_SIMPLE_OUTPUT_NAME = '%filename%_%stem%_%model%'

/**
 * Early Studio builds labelled the YAML save-map value as a filename even
 * though pymss defines it as an output subdirectory. Migrate generated values
 * to the current flat-file representation while leaving ordinary folder
 * names (which do not have an audio suffix) untouched.
 */
export function normalizeSimpleWorkflowDefinition(
  definition: Record<string, unknown>,
): Record<string, unknown> {
  if (!Array.isArray(definition.steps)) return definition
  let changed = false
  const base = { ...definition }
  if (Object.prototype.hasOwnProperty.call(base, 'save_intermediate')) {
    // Saving is represented by explicit links to the save node. Drop the
    // retired global switch when an older definition is loaded.
    delete base.save_intermediate
    changed = true
  }
  const defaults = isRecord(base.defaults) ? base.defaults : {}
  const outputFormat = typeof defaults.output_format === 'string' && defaults.output_format.trim()
    ? defaults.output_format.trim().toLowerCase()
    : 'wav'
  const steps = (definition.steps as unknown[]).map((value) => {
    if (!isRecord(value) || !isRecord(value.save)) return value
    let saveChanged = false
    const outputNames = isRecord(value.output_names) ? { ...value.output_names } : {}
    const save = Object.fromEntries(Object.entries(value.save).map(([stem, target]) => {
      if (typeof target !== 'string') return [stem, target]
      const trimmed = target.trim()
      if (LEGACY_SIMPLE_SAVE_FILENAME.test(trimmed)) {
        saveChanged = true
        outputNames[stem] = trimmed
        return [stem, 'Default']
      }
      // The current creator used the stem itself as the directory name. Treat
      // that generated convention as a flat output and preserve custom names.
      if (trimmed.localeCompare(stem, undefined, { sensitivity: 'accent' }) === 0) {
        saveChanged = true
        outputNames[stem] = outputNames[stem] || `${DEFAULT_SIMPLE_OUTPUT_NAME}.${outputFormat}`
        return [stem, 'Default']
      }
      return [stem, target]
    }))
    if (!saveChanged) return value
    changed = true
    return { ...value, save, output_names: outputNames }
  })
  return changed ? { ...base, steps } : definition
}

/**
 * Upgrade graph details whose serialized widget layout changed between pymss
 * versions. The returned definition is a copy only when a migration is needed.
 */
export function normalizeGraphWorkflowDefinition(
  definition: Record<string, unknown>,
): Record<string, unknown> {
  if (!Array.isArray(definition.nodes)) return definition

  let changed = false
  const nodes = definition.nodes.map((value) => {
    if (!isRecord(value)) return value
    const type = String(value.type || '').replace(/^pymss_/, '')
    if (type !== 'custom_mss_separate' && type !== 'custom_mss_separate_list') return value

    const widgets = value.widgets_values
    if (!Array.isArray(widgets)) return value
    const hasLegacyDownloadFields = typeof widgets[3] === 'boolean'
      && typeof widgets[4] === 'string'
    if (!hasLegacyDownloadFields) return value

    changed = true
    return {
      ...value,
      // Legacy layout:
      // [model, model_type, device, download_missing, source, device_ids, debug]
      // Current layout:
      // [model, model_type, device, device_ids, debug]
      widgets_values: [widgets[0], widgets[1], widgets[2], widgets[5], widgets[6] ?? false],
    }
  })

  return changed ? { ...definition, nodes } : definition
}
