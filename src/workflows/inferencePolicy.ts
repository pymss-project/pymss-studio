import { NODE_SPECS } from '../litegraph/nodeSpecs.ts'
import { comfySeparationNodeType, normalizeGraphWorkflowDefinition } from './formats.ts'
import {
  effectiveTtaOverride, resolveParameters, type CustomParameterIntent, type NativeParameters,
  type ParameterMode, type ParameterResolutionMetadata, type ResolvedParameters,
} from '../features/inference/parameters.ts'

export interface WorkflowParameterContext {
  mode: ParameterMode
  intent: CustomParameterIntent
  ttaOverride?: boolean
}

export function workflowModelName(value: unknown, nodeType = ''): string {
  const name = String(value || '').trim()
  const type = comfySeparationNodeType(nodeType)
  return type === 'pymss_custom_mss_separate' || type === 'pymss_custom_mss_separate_list'
    ? name : name.replace(/^\[[^\]]+\]\s*/, '')
}

export function findWorkflowModel<T extends { name: string; aliases?: string[]; modelPath?: string }>(
  entries: readonly T[], target: { model: string; modelPath?: string },
) {
  if (target.modelPath) {
    const path = target.modelPath.replaceAll('\\', '/')
    return entries.find(entry => entry.modelPath?.replaceAll('\\', '/') === path)
  }
  const name = target.model.toLowerCase()
  return entries.find(entry => entry.name.toLowerCase() === name || entry.aliases?.some(alias => alias.toLowerCase() === name))
}
export interface WorkflowParameterTarget {
  id: string
  model: string
  label?: string
  kind: 'mss' | 'vr' | 'custom'
  linked: boolean
  linkSource?: string
  dynamic?: boolean
  linkValues?: NativeParameters
  modelPath?: string
  configPath?: string
  modelType?: string
  modelDir?: string
  downloadMissing: boolean
  downloadSource?: string
  defaults: Record<string, unknown>
  overrides: NativeParameters
  /** Legacy YAML sentinels kept for an unchanged editor round trip. */
  modelDefaultKeys?: string[]
  /** Explicit null fields that the native compiler leaves to the model. */
  legacyNullFields?: string[]
}
export type WorkflowParameterSource = 'model-config' | 'runtime-default' | 'model-library' | 'custom' | 'workflow-default' | 'node' | 'link'
export interface WorkflowNodeParameters {
  id: string
  model: string
  label?: string
  parameters: NativeParameters
  useTta: boolean
  sources: Record<string, WorkflowParameterSource>
  skipped: string[]
  issues: ResolvedParameters['issues']
  adjustments: ResolvedParameters['adjustments']
  linked: boolean
  linkSource?: string
  dynamic?: boolean
  fingerprint?: string
  overrides: NativeParameters
  downloadMissing?: boolean
  downloadSource?: string
  /** A historical retry has already observed the upstream producer. */
  replay?: boolean
  nativeExtras?: Record<string, unknown>
  overlapCount?: number
  scaleModelOverlap?: boolean
}
export interface WorkflowParameterPlan {
  version: 1
  context: WorkflowParameterContext
  nodes: Record<string, WorkflowNodeParameters>
}

export type WorkflowObservedParameters = Pick<WorkflowNodeParameters, 'parameters' | 'sources' | 'useTta' | 'nativeExtras' | 'fingerprint'>

export function replayWorkflowParameters(
  plan: WorkflowParameterPlan, actual: Record<string, WorkflowObservedParameters> = {},
): WorkflowParameterPlan {
  const copy = JSON.parse(JSON.stringify(plan)) as WorkflowParameterPlan
  for (const node of Object.values(copy.nodes)) {
    const recorded = Object.hasOwn(actual, node.id) ? actual[node.id] : undefined
    if (recorded?.fingerprint) node.fingerprint = recorded.fingerprint
    if (node.dynamic && recorded) {
      node.parameters = { ...recorded.parameters }
      node.sources = { ...recorded.sources }
      node.useTta = recorded.useTta
      if (recorded.nativeExtras) node.nativeExtras = JSON.parse(JSON.stringify(recorded.nativeExtras))
      node.replay = true
      node.dynamic = false
    }
  }
  return copy
}

const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const fields = new Set(['batch_size', 'chunk_size', 'overlap_size', 'standardize', 'normalize', 'enable_tta',
  'window_size', 'aggression', 'enable_post_process', 'post_process_threshold', 'high_end_process'])
function isNativeField([key, value]: [string, unknown]) {
  return fields.has(key) && (typeof value === 'boolean' || typeof value === 'number' && Number.isFinite(value))
}
function nativeFields(raw: unknown): NativeParameters {
  return Object.fromEntries(Object.entries(record(raw)).filter(isNativeField)) as NativeParameters
}

function legacyNullParameters(params: Record<string, unknown>, kind: WorkflowParameterTarget['kind']) {
  const result = { ...params }
  const spec = NODE_SPECS[kind === 'vr' ? 'pymss_vr_params' : 'pymss_mss_params']!
  // The YAML compiler passes native widget defaults for explicit null scalars.
  for (const widget of spec.widgets) {
    const value = widget.default
    if (params[widget.name] === null && (typeof value === 'number' || typeof value === 'boolean')) {
      result[widget.name] = value
    }
  }
  return result
}

function isLegacySizeDefault(key: string, value: unknown, nativeOverlap: boolean) {
  if (key !== 'chunk_size' && key !== 'overlap_size') return false
  return value === null || (value === 0 && (key === 'chunk_size' || !nativeOverlap))
    || (typeof value === 'string' && value.trim().toLowerCase() === 'default')
}

export function simpleStepHasCustomParameters(params: unknown, nativeOverlap = false) {
  return Object.entries(record(params)).some(entry =>
    isNativeField(entry) && !isLegacySizeDefault(entry[0], entry[1], nativeOverlap))
}

export function isWorkflowInferenceConsumer(type: string) {
  return ['mss_separate', 'vr_separate', 'custom_mss_separate'].includes(type.replace(/^pymss_/, '').replace(/_list$/, ''))
}

export function explicitOverlapStepIds(studio: unknown): Set<string> {
  const ids = record(studio).nativeInferenceSteps
  return new Set(Array.isArray(ids) ? ids.filter((id): id is string => typeof id === 'string').map(id => id.trim()) : [])
}

export function isWorkflowParameterNode(type: string) {
  return type === 'pymss_mss_params' || type === 'pymss_vr_params'
}

/** Consumers save sparse overrides; native parameter nodes store every widget, including defaults. */
export function graphNodeHasCustomParameters(type: string, overrides: unknown, widgetValue: (index: number) => unknown) {
  if (isWorkflowInferenceConsumer(type)) return Object.entries(record(overrides)).some(isNativeField)
  if (!isWorkflowParameterNode(type)) return false
  return NODE_SPECS[type]!.widgets.some((widget, i) => {
    const value = widgetValue(i)
    if (value == null) return false
    if (widget.type === 'number' && typeof widget.default === 'number') {
      const number = Number(value)
      // Fractional arrows can return 0.20000000000000004 when stepped back to 0.2.
      return !Number.isFinite(number) || Math.abs(number - widget.default) > Number.EPSILON * Math.max(1, Math.abs(widget.default))
    }
    if (widget.type === 'toggle') return Boolean(value) !== widget.default
    const text = String(value).trim()
    return text !== '' && text.toLowerCase() !== String(widget.default).toLowerCase()
  })
}

export function workflowParameterTargets(definition: Record<string, unknown>): WorkflowParameterTarget[] {
  if (Array.isArray(definition.steps)) {
    const defaults = record(record(definition.defaults).inference_params)
    const nativeOverlapSteps = explicitOverlapStepIds(definition.studio)
    return definition.steps.map(record).map(step => {
      // The SDK trims identifiers and optional model fields before compiling YAML.
      const id = String(step.id).trim()
      const modelPath = String(step.model_path || '').trim()
      const modelType = String(step.model_type || '').trim()
      const kind = modelPath ? 'custom' : modelType.toLowerCase() === 'vr' ? 'vr' : 'mss'
      const original = record(step.inference_params)
      const nativeDefaults = legacyNullParameters(defaults, kind)
      const nativeOverrides = legacyNullParameters(original, kind)
      const overrides = nativeFields(nativeOverrides)
      const legacyNullFields = Object.entries({ ...nativeDefaults, ...nativeOverrides })
        .filter(([key, value]) => value === null && fields.has(key) && !isLegacySizeDefault(key, value, false))
        .map(([key]) => key)
      if (typeof step.use_tta === 'boolean' && overrides.enable_tta === undefined) overrides.enable_tta = step.use_tta
      const nativeOverlap = nativeOverlapSteps.has(id)
      const modelDefaultKeys = Object.keys(original).filter(key => isLegacySizeDefault(key, original[key], nativeOverlap))
      for (const key of modelDefaultKeys) delete overrides[key]
      return { id, model: workflowModelName(step.model) || modelPath, label: id, kind, modelType, modelPath,
        configPath: String(step.config_path || '').trim(), modelDir: String(step.model_dir || '').trim(),
        downloadMissing: !modelPath,
        linked: false, overrides, defaults: nativeDefaults, modelDefaultKeys,
        ...(legacyNullFields.length ? { legacyNullFields } : {}) }
    })
  }
  const normalized = normalizeGraphWorkflowDefinition(definition)
  if (!Array.isArray(normalized.nodes)) return []
  const nodes = normalized.nodes.map(record)
  const byId = new Map(nodes.map(node => [String(node.id), node]))
  const links = new Map<string, string>()
  for (const link of Array.isArray(definition.links) ? definition.links : []) {
    const edge = record(link)
    links.set(String(Array.isArray(link) ? link[0] : edge.id ?? edge[0]), String(Array.isArray(link) ? link[1] : edge.origin_id ?? edge[1]))
  }
  const targets: WorkflowParameterTarget[] = []
  for (const node of nodes) {
    const type = String(node.type || '').replace(/^pymss_/, '').replace(/_list$/, '')
    if (!isWorkflowInferenceConsumer(String(node.type))) continue
    const widgets = Array.isArray(node.widgets_values) ? node.widgets_values : []
    const props = record(node.properties)
    const model = workflowModelName(widgets[0] || props.model_name || node.model_path, String(node.type))
    const ports = Array.isArray(node.inputs) ? node.inputs.map(record) : []
    const linkId = ports.find(port => port.name === 'params' || ['PYMSS_MSS_PARAMS', 'PYMSS_VR_PARAMS'].includes(String(port.type)))?.link
    const linked = linkId != null
    const sourceId = linked ? links.get(String(linkId)) : undefined
    const producer = sourceId === undefined ? undefined : byId.get(sourceId)
    const spec = NODE_SPECS[String(producer?.type)]
    const known = Boolean(producer && spec && ['pymss_mss_params', 'pymss_vr_params'].includes(spec.type)
      && !(Array.isArray(producer.inputs) && producer.inputs.some(port => record(port).link != null)))
    const linkValues: NativeParameters = {}
    if (known) {
      const values = Array.isArray(producer!.widgets_values) ? producer!.widgets_values : []
      spec.widgets.forEach((widget, index) => {
        if (!fields.has(widget.name)) return
        const value = values[index] ?? widget.default
        if (widget.type === 'toggle') {
          linkValues[widget.name] = Boolean(value)
          return
        }
        if (typeof value === 'string' && !value.trim()) return
        const number = Number(value)
        if (Number.isFinite(number) && (widget.name !== 'chunk_size' || number !== 0)) {
          linkValues[widget.name] = number
        }
      })
    }
    const downloadSource = ['modelscope', 'huggingface', 'hf-mirror'].includes(String(widgets[3]))
      ? String(widgets[3]) : undefined
    targets.push({ id: String(node.id), model, label: String(node.title || model || node.id),
      kind: type === 'vr_separate' ? 'vr' : type === 'custom_mss_separate' ? 'custom' : 'mss',
      modelType: type === 'custom_mss_separate' ? String(widgets[1] || '') : undefined,
      modelPath: String(node.model_path || ''), configPath: String(node.config_path || ''),
      downloadMissing: type !== 'custom_mss_separate' && !node.model_path && widgets[2] !== false,
      downloadSource: type !== 'custom_mss_separate' ? downloadSource : undefined,
      linked, linkSource: linked ? String(producer?.title || producer?.type || 'params') + (sourceId ? ' #' + sourceId : '') : undefined,
      dynamic: linked && !known, linkValues, defaults: {}, overrides: nativeFields(props.studioInferenceOverrides) })
  }
  return targets
}

const semanticKeys = {
  chunk_size: 'chunkMilliseconds', overlap_size: 'overlapBasisPoints', batch_size: 'batchSize',
  standardize: 'standardize', normalize: 'normalize',
} as const

export function resolveWorkflowNode(
  target: WorkflowParameterTarget, metadata: ParameterResolutionMetadata | undefined,
  library: Record<string, unknown> | undefined, context: WorkflowParameterContext,
): WorkflowNodeParameters {
  // Resolve the semantic intent against the effective block length. Only a
  // field that is actually bound by a step, workflow default or link masks it.
  const defaults = nativeFields(target.defaults)
  for (const key of ['chunk_size', 'overlap_size']) {
    if (defaults[key] === 0) delete defaults[key]
  }
  // A legacy step's explicit Default masks the same workflow-level field.
  for (const key of target.modelDefaultKeys ?? []) delete defaults[key]
  const pinned = { ...defaults, ...target.linkValues, ...target.overrides }
  const intent = { ...context.intent, vr: { ...context.intent.vr } }
  const inheritedLibrary = { ...library }
  const legacyNullFields = target.legacyNullFields ?? []
  for (const key of legacyNullFields) {
    delete pinned[key]
    delete inheritedLibrary[key]
  }
  for (const key of [...Object.keys(pinned), ...legacyNullFields]) {
    const semantic = semanticKeys[key as keyof typeof semanticKeys]
    if (semantic) delete intent[semantic]
    else delete intent.vr[key as keyof typeof intent.vr]
  }
  const resolved = resolveParameters(metadata, { ...inheritedLibrary, ...pinned }, context.mode, intent)
  if (!metadata && (context.mode === 'model' || (
    !Object.keys(intent.vr).length && !Object.values(semanticKeys).some(key => intent[key] !== undefined)
  ))) resolved.issues = resolved.issues.filter(issue => issue.code !== 'metadataUnavailable')
  const parameters = metadata ? resolved.parameters : nativeFields(inheritedLibrary)
  const sources: WorkflowNodeParameters['sources'] = { ...resolved.sources }
  if (!metadata) {
    for (const key of Object.keys(parameters)) sources[key] = 'model-library'
  }
  for (const [key, value] of Object.entries(pinned)) {
    if (key === 'enable_tta') continue
    if (metadata?.capabilities[key]?.kind === 'unsupported') {
      if (!resolved.skipped.includes(key)) resolved.skipped.push(key)
      continue
    }
    const baseline = metadata?.defaults[key]
    if (baseline !== undefined && (typeof value !== typeof baseline ||
      (metadata?.capabilities[key]?.kind === 'fixed' && value !== baseline))) {
      resolved.issues.push({ key, code: 'invalidValue' })
    }
    parameters[key] = value
    sources[key] = Object.hasOwn(target.overrides, key) ? 'node'
      : Object.hasOwn(target.linkValues || {}, key) ? 'link' : 'workflow-default'
  }
  // An absolute overlap from the workflow may exceed a shorter custom block.
  if (metadata && Object.keys(pinned).length) {
    const checked = resolveParameters(metadata, parameters, 'model', {})
    for (const issue of checked.issues) {
      if (!resolved.issues.some(other => other.key === issue.key && other.code === issue.code)) resolved.issues.push(issue)
    }
  }
  const globalTta = effectiveTtaOverride(context.mode, context.ttaOverride)
  const useTta = typeof pinned.enable_tta === 'boolean' ? pinned.enable_tta : globalTta ?? false
  sources.enable_tta = Object.hasOwn(target.overrides, 'enable_tta') ? 'node'
    : Object.hasOwn(target.linkValues || {}, 'enable_tta') ? 'link'
    : Object.hasOwn(defaults, 'enable_tta') ? 'workflow-default'
    : globalTta !== undefined ? 'custom' : 'runtime-default'
  const overlapCount = library?.overlap_size === undefined && metadata?.overlap.kind === 'count'
    ? Number(library?.num_overlap ?? metadata.overlap.count) : undefined
  return { ...resolved, id: target.id, model: target.model, label: target.label, parameters, useTta,
    sources, linked: target.linked, dynamic: target.dynamic, linkSource: target.linkSource,
    downloadMissing: target.downloadMissing, downloadSource: target.downloadSource,
    overrides: Object.fromEntries(Object.entries(target.overrides).filter(([key]) => sources[key] === 'node')),
    ...(overlapCount && Number.isSafeInteger(overlapCount) && overlapCount > 0 ? { overlapCount } : {}),
    ...(target.dynamic && metadata?.overlap.kind === 'samples' && library?.overlap_size === undefined
      ? { scaleModelOverlap: true } : {}),
    fingerprint: metadata?.fingerprint }
}
