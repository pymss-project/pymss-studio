import {
  detectWorkflowFormat,
  hasInvalidSimpleStructure,
  isWorkflowSaveNodeType,
  isWorkflowSeparationNodeType,
} from '@/workflows/formats'
import { analyzeWorkflowInputs } from '@/utils/workflowInputs'

/**
 * Simple creator <-> pymss YAML workflow adapter.
 *
 * The simple creator edits a linear list of steps (one model per step, each
 * step consumes `input` or a previous step's stem, and saves chosen stems).
 * We now store this directly as a pymss YAML workflow dict:
 *
 *   { version: 1, defaults: { device, output_format, inference_params },
 *     steps: [ { id, model, input, stems, save, output_names, ... } ] }
 *
 * pymss.workflow.load_workflow_data parses this and compile_workflow_to_dag
 * builds the DAG that run_dag executes. ``save`` keeps pymss' directory
 * contract while ``output_names`` is consumed by the Studio worker to wire
 * user-facing filename hints into each save node.
 */
export type SimpleWorkflowSavePayload = {
  id?: string
  name: string
  description: string
  definition: Record<string, unknown>
  expectedUpdatedAt?: number
}

export type SimpleWorkflowReasonCode =
  | 'graph_workflow'
  | 'advanced_parameters'
  | 'invalid_definition'

export type PymssYamlStep = {
  id: string
  model: string
  input: string
  stems: string[]
  save: Record<string, unknown>
  /** Studio-only filename templates keyed by output stem. */
  output_names?: Record<string, string>
  inference_params?: Record<string, unknown>
  model_type?: string
  model_path?: string
  config_path?: string
  model_dir?: string
  device?: string
  output_format?: string
  use_tta?: boolean
}

export type PymssYamlWorkflow = {
  version: number
  defaults: {
    device: string
    output_format: string
    model_dir?: string | null
    inference_params?: Record<string, unknown>
  }
  /** Studio-only canvas state; ignored by pymss at runtime. */
  studio?: SimpleEditorUi
  steps: PymssYamlStep[]
}

export type SimpleStepDraft = {
  id: string
  model: string
  input: string
  stems: string[]
  save: Record<string, string>
  outputNames: Record<string, string>
}

export type SimpleEditorPoint = { x: number; y: number }

export type SimpleEditorBounds = {
  x: number
  y: number
  width: number
  height: number
}

export type SimpleEditorUi = {
  editor: 'simple'
  viewport: { x: number; y: number; zoom: number }
  nodes: Record<string, SimpleEditorPoint>
}

/** Calculate a centered viewport that contains all visible simple-editor nodes. */
export function fitSimpleEditorViewport(
  bounds: SimpleEditorBounds[],
  canvasWidth: number,
  canvasHeight: number,
  options: { padding?: number; minZoom?: number; maxZoom?: number } = {},
) {
  const validBounds = bounds.filter(item => [item.x, item.y, item.width, item.height].every(Number.isFinite))
  if (!validBounds.length || !Number.isFinite(canvasWidth) || !Number.isFinite(canvasHeight) || canvasWidth <= 0 || canvasHeight <= 0) {
    return { x: 0, y: 0, zoom: 1 }
  }

  const paddingValue = Number(options.padding ?? 48)
  const minZoomValue = Number(options.minZoom ?? 0.35)
  const maxZoomValue = Number(options.maxZoom ?? 1.8)
  const padding = Number.isFinite(paddingValue) ? Math.max(0, paddingValue) : 48
  const minZoom = Number.isFinite(minZoomValue) ? Math.max(0.01, minZoomValue) : 0.35
  const maxZoom = Number.isFinite(maxZoomValue) ? Math.max(minZoom, maxZoomValue) : Math.max(minZoom, 1.8)
  const left = Math.min(...validBounds.map(item => item.x))
  const top = Math.min(...validBounds.map(item => item.y))
  const right = Math.max(...validBounds.map(item => item.x + Math.max(0, item.width)))
  const bottom = Math.max(...validBounds.map(item => item.y + Math.max(0, item.height)))
  const contentWidth = Math.max(1, right - left)
  const contentHeight = Math.max(1, bottom - top)
  const availableWidth = Math.max(1, canvasWidth - padding * 2)
  const availableHeight = Math.max(1, canvasHeight - padding * 2)
  const zoom = Math.min(maxZoom, Math.max(minZoom, Math.min(availableWidth / contentWidth, availableHeight / contentHeight)))

  return {
    x: (canvasWidth - contentWidth * zoom) / 2 - left * zoom,
    y: (canvasHeight - contentHeight * zoom) / 2 - top * zoom,
    zoom,
  }
}

export type SimpleDraft = {
  defaultDevice: string
  defaultFormat: string
  defaultNormalize: boolean
  steps: SimpleStepDraft[]
  ui: SimpleEditorUi
}

const isRecord = (v: unknown): v is Record<string, unknown> => Boolean(v) && typeof v === 'object' && !Array.isArray(v)

export type ComfyOverview = {
  nodeCount: number
  linkCount: number
  separateCount: number
  outputCount: number
  models: string[]
  inputSlots: string[]
}

/** Read-only overview of a comfy-mss graph for the workflows page: node /
 * separation / output counts, referenced models, and runtime input slots
 * (load nodes with an input_name widget — pymss >= 2.1.2 contract). */
export function analyzeComfyOverview(definition: unknown): ComfyOverview | null {
  if (!isRecord(definition) || !Array.isArray(definition.nodes)) return null
  const nodes = definition.nodes as any[]
  const stripPrefix = (raw: string) => raw.replace(/^\[[^\]]*\]\s*/, '').trim()
  const models: string[] = []
  let separateCount = 0
  let outputCount = 0
  for (const node of nodes) {
    const type = String(node?.type || '')
    if (isWorkflowSeparationNodeType(type)) {
      separateCount += 1
      const raw = String(node.widgets_values?.[0] || '').trim()
      const model = stripPrefix(raw)
      if (model && !models.includes(model)) models.push(model)
    }
    if (isWorkflowSaveNodeType(type)) outputCount += 1
  }
  return {
    nodeCount: nodes.length,
    linkCount: Array.isArray(definition.links) ? definition.links.length : 0,
    separateCount,
    outputCount,
    models,
    inputSlots: analyzeWorkflowInputs(definition).slots.map(slot => slot.name),
  }
}

/** Read a pymss YAML workflow dict into the editor's draft shape. */
export function hydrateSimpleWorkflow(definition: unknown): SimpleDraft {
  if (!isRecord(definition) || !Array.isArray(definition.steps)) {
    return {
      defaultDevice: 'auto', defaultFormat: 'wav', defaultNormalize: false,
      steps: [], ui: createDefaultSimpleEditorUi(),
    }
  }
  const defaults = isRecord(definition.defaults) ? definition.defaults : {}
  const inference = isRecord(defaults.inference_params) ? defaults.inference_params : {}
  const steps = (definition.steps as any[])
    .filter(isRecord)
    .map((raw, index): SimpleStepDraft => ({
      id: String(raw.id || `step${index + 1}`),
      model: String(raw.model || ''),
      input: String(raw.input || 'input'),
      stems: raw.stems == null
        ? []
        : (Array.isArray(raw.stems) ? raw.stems : [raw.stems]).map(s => String(s)),
      save: isRecord(raw.save)
        ? Object.fromEntries(Object.entries(raw.save)
          .filter(([, value]) => value !== false && value !== null && value !== undefined && Boolean(String(value).trim()))
          .map(([k, v]) => [k, String(v)]))
        : {},
      outputNames: isRecord(raw.output_names)
        ? Object.fromEntries(Object.entries(raw.output_names).map(([k, v]) => [k, String(v)]))
        : {},
    }))
  return {
    defaultDevice: String(defaults.device || 'auto'),
    defaultFormat: String(defaults.output_format || 'wav'),
    defaultNormalize: Boolean(inference.normalize),
    steps,
    ui: hydrateSimpleEditorUi(definition.studio, steps),
  }
}

/** Build a pymss YAML workflow dict from the editor draft. */
export function buildSimpleWorkflowDefinition(draft: SimpleDraft): PymssYamlWorkflow {
  return {
    version: 1,
    defaults: {
      device: draft.defaultDevice || 'auto',
      output_format: draft.defaultFormat || 'wav',
      inference_params: { normalize: Boolean(draft.defaultNormalize) },
    },
    studio: normalizeSimpleEditorUi(draft.ui, draft.steps),
    steps: draft.steps.map((step, index) => ({
      id: step.id || `step${index + 1}`,
      model: step.model,
      input: step.input || 'input',
      stems: [...step.stems],
      save: { ...step.save },
      output_names: { ...step.outputNames },
    })),
  }
}

export function createDefaultSimpleEditorUi(steps: SimpleStepDraft[] = []): SimpleEditorUi {
  const nodes: Record<string, SimpleEditorPoint> = {
    input: { x: 64, y: 220 },
    save: { x: 1040, y: 220 },
  }
  steps.forEach((step, index) => {
    nodes[step.id || `step${index + 1}`] = { x: 360 + index * 330, y: 190 + (index % 2) * 140 }
  })
  return { editor: 'simple', viewport: { x: 0, y: 0, zoom: 1 }, nodes }
}

function hydrateSimpleEditorUi(value: unknown, steps: SimpleStepDraft[]): SimpleEditorUi {
  const fallback = createDefaultSimpleEditorUi(steps)
  if (!isRecord(value)) return fallback
  const viewportValue = isRecord(value.viewport) ? value.viewport : {}
  const nodeValues = isRecord(value.nodes) ? value.nodes : {}
  const x = Number(viewportValue.x)
  const y = Number(viewportValue.y)
  const zoom = Number(viewportValue.zoom)
  const nodes = { ...fallback.nodes }
  Object.entries(nodeValues).forEach(([id, point]) => {
    if (!isRecord(point)) return
    const px = Number(point.x)
    const py = Number(point.y)
    if (Number.isFinite(px) && Number.isFinite(py)) nodes[id] = { x: Math.round(px), y: Math.round(py) }
  })
  steps.forEach((step, index) => {
    const id = step.id || `step${index + 1}`
    if (!nodes[id]) nodes[id] = { x: 360 + index * 330, y: 190 + (index % 2) * 140 }
  })
  return {
    editor: 'simple',
    viewport: {
      x: Number.isFinite(x) ? x : fallback.viewport.x,
      y: Number.isFinite(y) ? y : fallback.viewport.y,
      zoom: Number.isFinite(zoom) && zoom > 0 ? Math.min(2, Math.max(0.35, zoom)) : fallback.viewport.zoom,
    },
    nodes,
  }
}

function normalizeSimpleEditorUi(value: SimpleEditorUi | undefined, steps: SimpleStepDraft[]): SimpleEditorUi {
  const hydrated = hydrateSimpleEditorUi(value, steps)
  const validIds = new Set(['input', 'save', ...steps.map((step, index) => step.id || `step${index + 1}`)])
  hydrated.nodes = Object.fromEntries(Object.entries(hydrated.nodes).filter(([id]) => validIds.has(id)))
  return hydrated
}

export function createStepDraft(index: number): SimpleStepDraft {
  return {
    id: `step${index + 1}`,
    model: '',
    input: index === 0 ? 'input' : '',
    stems: [],
    save: {},
    outputNames: {},
  }
}

const SIMPLE_FILENAME_TOKENS = /%([A-Za-z_][A-Za-z0-9_]*)%/g
const SIMPLE_AUDIO_SUFFIX = /\.(?:wav|flac|mp3|m4a)$/i
const SIMPLE_INVALID_FILENAME_CHARS = /[\u0000-\u001f<>:"/\\|?*]+/g
const SIMPLE_WINDOWS_RESERVED_NAMES = new Set([
  'CON', 'PRN', 'AUX', 'NUL',
  ...Array.from({ length: 9 }, (_, index) => `COM${index + 1}`),
  ...Array.from({ length: 9 }, (_, index) => `LPT${index + 1}`),
])

export type SimpleOutputFilenamePreviewOptions = {
  stem: string
  model?: string
  stepId?: string
  index?: number
  inputName?: string
  outputFormat?: string
}

function filenamePart(value: unknown, fallback: string) {
  const text = String(value || '').trim()
  if (!text) return fallback
  const basename = text.split(/[\\/]/).pop() || text
  return basename.replace(/\.[^.]+$/, '') || fallback
}

/**
 * Render the filename shown in the simple editor's save-node preview.
 *
 * The editor does not have a runtime input file yet, so `%filename%` uses
 * `input.wav` as a stable example. Keep this in sync with the worker's
 * `_render_simple_filename` sanitizer so users see the same extension and
 * invalid-character handling before they run the workflow.
 */
export function renderSimpleOutputFilename(
  template: unknown,
  options: SimpleOutputFilenamePreviewOptions,
) {
  const inputName = filenamePart(options.inputName, 'input')
  const model = filenamePart(options.model, 'model')
  const stem = String(options.stem || '').trim() || 'output'
  const stepId = String(options.stepId || '').trim() || 'step'
  const index = String(Math.max(1, Number(options.index) || 1))
  const format = String(options.outputFormat || 'wav').trim().replace(/^\./, '').toLowerCase() || 'wav'
  const replacements: Record<string, string> = {
    filename: inputName,
    track: inputName,
    stem,
    model,
    step: stepId,
    index,
  }
  const rawTemplate = String(template || '%filename%_%stem%_%model%').trim()
  const withoutSuffix = rawTemplate.replace(SIMPLE_AUDIO_SUFFIX, '')
  const rendered = withoutSuffix.replace(SIMPLE_FILENAME_TOKENS, (token, key: string) => replacements[key.toLowerCase()] || token)
  const safe = rendered.replace(SIMPLE_INVALID_FILENAME_CHARS, '_').trim().replace(/^[ .]+|[ .]+$/g, '')
    || stem
    || 'output'
  const basename = safe.split('.', 1)[0].toUpperCase()
  const normalized = SIMPLE_WINDOWS_RESERVED_NAMES.has(basename) ? `_${safe}` : safe
  return `${normalized}.${format}`
}

/** Split a catalog model's instruments/target stem into a deduped stem list. */
export function parseModelStems(value?: unknown): string[] {
  const seen = new Set<string>()
  const rawItems = Array.isArray(value)
    ? value
    : String(value || '').split(/[,\uFF0C;\uFF1B/|\n]+/)
  return rawItems
    .map(item => String(item || '').trim().replace(/^[\s"'\[\](){}]+|[\s"'\[\](){}]+$/g, ''))
    .filter((item) => {
      if (!item) return false
      const key = item.toLowerCase()
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
}

/** Stems offered for a model, derived from the catalog entry fields. */
export function configuredStemsFor(model?: ModelEntryLike): string[] {
  return parseModelStems(model?.configInstruments || model?.configTargetInstrument || model?.targetStem)
}

type ModelEntryLike = {
  configInstruments?: string
  configTargetInstrument?: string
  targetStem?: string
}

const SIMPLE_DEFINITION_FIELDS = new Set(['version', 'defaults', 'steps', 'studio'])
const SIMPLE_DEFAULT_FIELDS = new Set(['device', 'output_format', 'inference_params'])
const SIMPLE_INFERENCE_FIELDS = new Set(['normalize'])
const SIMPLE_STEP_FIELDS = new Set(['id', 'model', 'input', 'stems', 'save', 'output_names'])

function hasUnsupportedFields(value: Record<string, unknown>, allowed: Set<string>): boolean {
  return Object.keys(value).some(key => !allowed.has(key))
}

function hasInvalidSimpleEditorUi(value: unknown): boolean {
  if (value == null) return false
  if (!isRecord(value) || value.editor !== 'simple') return true
  if (!isRecord(value.viewport) || !isRecord(value.nodes)) return true
  const viewport = value.viewport
  if (![viewport.x, viewport.y, viewport.zoom].every(item => typeof item === 'number' && Number.isFinite(item))) return true
  return Object.values(value.nodes).some(point => (
    !isRecord(point)
    || typeof point.x !== 'number'
    || typeof point.y !== 'number'
    || !Number.isFinite(point.x)
    || !Number.isFinite(point.y)
  ))
}

function usesAdvancedSimpleParameters(definition: Record<string, unknown>): boolean {
  if (hasUnsupportedFields(definition, SIMPLE_DEFINITION_FIELDS)) return true
  if (hasInvalidSimpleEditorUi(definition.studio)) return true
  const defaults = isRecord(definition.defaults) ? definition.defaults : {}
  if (hasUnsupportedFields(defaults, SIMPLE_DEFAULT_FIELDS)) return true

  const inference = isRecord(defaults.inference_params) ? defaults.inference_params : {}
  if (hasUnsupportedFields(inference, SIMPLE_INFERENCE_FIELDS)) return true

  return (definition.steps as unknown[]).some((value) => {
    if (!isRecord(value) || hasUnsupportedFields(value, SIMPLE_STEP_FIELDS)) return true
    if (value.save != null && !isRecord(value.save)) return true
    if (isRecord(value.save) && Object.values(value.save).some(target => typeof target !== 'string')) return true
    if (value.output_names != null && !isRecord(value.output_names)) return true
    return isRecord(value.output_names)
      && Object.values(value.output_names).some(target => typeof target !== 'string')
  })
}

/**
 * The simple creator deliberately edits only the compact subset represented by
 * {@link SimpleDraft}. Definitions with per-step overrides or custom model
 * paths must be promoted instead of being re-saved through a lossy form.
 */
export function analyzeSimpleWorkflow(definition: unknown): { editable: boolean; reasonCodes: SimpleWorkflowReasonCode[] } {
  const format = detectWorkflowFormat(definition)
  if (format === 'graph') return { editable: false, reasonCodes: ['graph_workflow'] }
  if (format !== 'simple' || !isRecord(definition)) {
    return { editable: false, reasonCodes: ['invalid_definition'] }
  }
  if (hasInvalidSimpleStructure(definition)) {
    return { editable: false, reasonCodes: ['invalid_definition'] }
  }
  if (usesAdvancedSimpleParameters(definition)) {
    return { editable: false, reasonCodes: ['advanced_parameters'] }
  }
  return { editable: true, reasonCodes: [] }
}

export function resolveWorkflowOpenMode(definition: unknown): 'simple' | 'advanced' {
  return analyzeSimpleWorkflow(definition).editable ? 'simple' : 'advanced'
}
