import {
  detectWorkflowFormat,
  hasInvalidSimpleStructure,
  isWorkflowSaveNodeType,
  normalizeGraphWorkflowDefinition,
} from '@/workflows/formats'

export type WorkflowDefinitionIssue =
  | 'steps-required'
  | 'no-save-outputs'
  | 'invalid-definition'
  | 'invalid-format'
  | null

export type WorkflowRuntimeDefaults = {
  device: string
  outputFormat: string
  modelDir: string | null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function nonEmptyString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

export function resolveWorkflowRuntimeDefaults(
  definition: Record<string, unknown>,
  fallback: WorkflowRuntimeDefaults,
): WorkflowRuntimeDefaults {
  const format = detectWorkflowFormat(definition)
  const source = format === 'simple'
    ? (isRecord(definition.defaults) ? definition.defaults : {})
    : format === 'graph' && isRecord(definition.extra) && isRecord(definition.extra.appDefaults)
      ? definition.extra.appDefaults
      : {}
  return {
    device: nonEmptyString(source.device) || fallback.device,
    outputFormat: (nonEmptyString(source.output_format) || fallback.outputFormat).toLowerCase(),
    modelDir: nonEmptyString(source.model_dir) || fallback.modelDir,
  }
}

export function mergeSimpleInferenceParams(
  defaults: Record<string, unknown>,
  step: Record<string, unknown>,
  stepUseTta?: boolean,
): Record<string, unknown> {
  const merged = { ...defaults, ...step }
  if (stepUseTta !== undefined && step.enable_tta === undefined) {
    merged.enable_tta = stepUseTta
  }
  return merged
}

export function getWorkflowDefinitionIssue(
  definition: Record<string, unknown>,
): WorkflowDefinitionIssue {
  const format = detectWorkflowFormat(definition)
  if (format === 'simple') {
    const steps = definition.steps as unknown[]
    if (hasInvalidSimpleStructure(definition)) return 'invalid-definition'
    if (!steps.length && !(Array.isArray(definition.ensembles) && definition.ensembles.length)) return 'steps-required'
    return countWorkflowSaveOutputs(definition) ? null : 'no-save-outputs'
  }

  if (format === 'graph') {
    const nodes = definition.nodes as unknown[]
    if (!nodes.length) return 'steps-required'
    return countWorkflowSaveOutputs(definition) ? null : 'no-save-outputs'
  }

  return 'invalid-format'
}

export function countWorkflowSaveOutputs(definition: Record<string, unknown>): number {
  const format = detectWorkflowFormat(definition)
  if (format === 'simple') {
    const stepOutputs = (definition.steps as unknown[]).reduce<number>((total, value) => {
      if (!isRecord(value) || !isRecord(value.save)) return total
      // pymss skips false/null/empty save targets. Count only entries that
      // produce at least one directory so the validation state matches the
      // worker's actual output behaviour.
      const outputCount = Object.values(value.save).filter((target) => {
        if (target === null || target === undefined || target === false) return false
        if (Array.isArray(target)) return target.some(item => String(item || '').trim())
        if (typeof target === 'string') return Boolean(target.trim())
        return Boolean(target)
      }).length
      return total + outputCount
    }, 0)
    const ensembleOutputs = (Array.isArray(definition.ensembles) ? definition.ensembles : [])
      .filter(value => (
        isRecord(value)
        && value.save !== false
        && value.save !== null
        && value.save !== undefined
        && Boolean(String(value.save).trim())
      )).length
    return stepOutputs + ensembleOutputs
  }
  if (format === 'graph') {
    return (definition.nodes as unknown[]).filter(value => (
      isRecord(value) && isWorkflowSaveNodeType(value.type)
    )).length
  }
  return 0
}

function materializeSimpleDefaults(
  definition: Record<string, unknown>,
  modelInferenceParams: Record<string, Record<string, unknown>> = {},
): Record<string, unknown> {
  const defaults = isRecord(definition.defaults) ? definition.defaults : {}
  const defaultInference = isRecord(defaults.inference_params) ? defaults.inference_params : {}
  const defaultDevice = typeof defaults.device === 'string' ? defaults.device.trim() : ''
  const defaultOutputFormat = typeof defaults.output_format === 'string'
    ? defaults.output_format.trim().toLowerCase()
    : ''

  const steps = (Array.isArray(definition.steps) ? definition.steps : []).map((value) => {
    if (!isRecord(value)) return value
    const modelName = nonEmptyString(value.model)
    const globalModelInference = isRecord(modelInferenceParams[modelName])
      ? modelInferenceParams[modelName]
      : {}
    const inheritedInference = { ...globalModelInference, ...defaultInference }
    const stepInference = isRecord(value.inference_params) ? value.inference_params : null
    const inheritInference = value.inference_params == null || stepInference !== null
    const inheritDevice = value.device == null
      || (typeof value.device === 'string' && !value.device.trim())
    const inheritOutputFormat = value.output_format == null
      || (typeof value.output_format === 'string' && !value.output_format.trim())
    return {
      ...value,
      ...(!inheritDevice || !defaultDevice ? {} : { device: defaultDevice }),
      ...(!inheritOutputFormat || !defaultOutputFormat ? {} : { output_format: defaultOutputFormat }),
      ...(inheritInference && (stepInference || Object.keys(inheritedInference).length)
        ? {
            inference_params: mergeSimpleInferenceParams(
              inheritedInference,
              stepInference || {},
              typeof value.use_tta === 'boolean' ? value.use_tta : undefined,
            ),
          }
        : {}),
    }
  })

  return { ...definition, steps }
}

/**
 * Build the transient definition sent to the worker. Persisted definitions
 * remain concise; runtime copies receive values required by pymss' DAG nodes.
 */
export function prepareWorkflowDefinitionForRun(
  definition: Record<string, unknown>,
  runtimeDefaults: {
    device: string
    outputFormat: string
    modelInferenceParams?: Record<string, Record<string, unknown>>
  },
): Record<string, unknown> {
  const clone = normalizeGraphWorkflowDefinition(
    JSON.parse(JSON.stringify(definition)) as Record<string, unknown>,
  )
  const format = detectWorkflowFormat(clone)
  if (format === 'simple') {
    if (clone.defaults == null || isRecord(clone.defaults)) {
      const defaults = isRecord(clone.defaults) ? clone.defaults : {}
      clone.defaults = {
        ...defaults,
        device: typeof defaults.device === 'string' && defaults.device.trim()
          ? defaults.device
          : runtimeDefaults.device,
        output_format: typeof defaults.output_format === 'string' && defaults.output_format.trim()
          ? defaults.output_format
          : runtimeDefaults.outputFormat,
      }
    }
    return materializeSimpleDefaults(clone, runtimeDefaults.modelInferenceParams)
  }
  if (format !== 'graph') return clone

  const nodes = Array.isArray(clone.nodes) ? clone.nodes as Record<string, unknown>[] : []
  for (const node of nodes) {
    if (String(node.type || '') !== 'pymss_save_audio') continue
    const widgets = Array.isArray(node.widgets_values) ? node.widgets_values : []
    while (widgets.length <= 0) widgets.push(null)
    if (!String(widgets[0] || '').trim() && runtimeDefaults.outputFormat) {
      widgets[0] = runtimeDefaults.outputFormat.toLowerCase()
    }
    node.widgets_values = widgets
  }
  return clone
}
