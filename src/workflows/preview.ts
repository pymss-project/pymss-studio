import { hydrateSimpleWorkflow, isSimpleAudioOperation } from '../utils/workflowSimple'
import { comfyLinksToLitegraph } from '../litegraph/graphAdapter'
import { BUILTIN_SPECS, NODE_SPECS } from '../litegraph/nodeSpecs'
import { translateNodeTitle } from '../litegraph/nodeLocalization'
import { graphNodeHasCustomParameters, simpleStepHasCustomParameters } from './inferencePolicy'
import { comfySeparationNodeType, isGraphWorkflowDefinition, isSimpleWorkflowDefinition, isWorkflowSaveNodeType, isWorkflowSeparationNodeType } from './formats'

export type PreviewNodeKind = 'input' | 'separate' | 'process' | 'output' | 'other'
export type WorkflowPreviewNode = {
  id: string
  title: string
  detail: string
  kind: PreviewNodeKind
  customParameters?: boolean
  x: number
  y: number
  width: number
  height: number
  inputs: number
  outputs: number
}
export type WorkflowPreviewLink = {
  id: string
  source: string
  target: string
  sourceSlot: number
  targetSlot: number
  type: string
}
export type WorkflowPreview = {
  nodes: WorkflowPreviewNode[]
  links: WorkflowPreviewLink[]
}
type Translate = (key: string) => string
const isRecord = (value: unknown): value is Record<string, unknown> => (
  Boolean(value) && typeof value === 'object' && !Array.isArray(value)
)

function makeNode(id: string, title: string, detail: string, kind: PreviewNodeKind): WorkflowPreviewNode {
  return { id, title, detail, kind, x: 0, y: 0, width: 212, height: 98, inputs: 0, outputs: 0 }
}

function outputReference(id: string, stem: string) {
  return `${id.trim()}.${stem.trim()}`
}

/** Imported graphs may omit canvas positions. Lay those out by dependency. */
function layoutNodes(graph: WorkflowPreview, positions: Map<string, { x: number; y: number }>) {
  const targets = new Map(graph.nodes.map(node => [node.id, new Set<string>()]))
  const degrees = new Map(graph.nodes.map(node => [node.id, 0]))
  const ranks = new Map(graph.nodes.map(node => [node.id, 0]))
  for (const link of graph.links) {
    const next = targets.get(link.source)!
    if (next.has(link.target)) continue
    next.add(link.target)
    degrees.set(link.target, degrees.get(link.target)! + 1)
  }
  const queue = graph.nodes.filter(node => degrees.get(node.id) === 0).map(node => node.id)
  for (let index = 0; index < queue.length; index++) {
    const id = queue[index]
    for (const target of targets.get(id)!) {
      ranks.set(target, Math.max(ranks.get(target)!, ranks.get(id)! + 1))
      degrees.set(target, degrees.get(target)! - 1)
      if (degrees.get(target) === 0) queue.push(target)
    }
  }
  const rows = new Map<number, number>()
  for (const node of graph.nodes) {
    const rank = ranks.get(node.id)!
    const row = rows.get(rank) || 0
    rows.set(rank, row + 1)
    const position = positions.get(node.id)
    node.x = position?.x ?? rank * 272
    node.y = position?.y ?? row * 146
  }
}

function buildSimplePreview(definition: Record<string, unknown>, t: Translate): WorkflowPreview {
  const draft = hydrateSimpleWorkflow(definition)
  const graph: WorkflowPreview = { nodes: [], links: [] }
  if (!draft.steps.length && !draft.ensembles.length) return graph
  const input = makeNode('input', t('workflows.originalInput'), '', 'input')
  input.outputs = 1
  graph.nodes.push(input)
  const sources = new Map<string, { id: string; slot: number }>([['input', { id: input.id, slot: 0 }]])
  const saved: string[] = []
  for (const step of draft.steps) {
    const node = makeNode(`step:${step.id}`, step.model || t('workflows.previewModelUnassigned'), step.stems.join(' · '), 'separate')
    if (simpleStepHasCustomParameters(step.inferenceParams, draft.ui.nativeInferenceSteps?.includes(step.id))) node.customParameters = true
    node.inputs = 1
    node.outputs = step.stems.length
    graph.nodes.push(node)
    step.stems.forEach((stem, slot) => sources.set(outputReference(step.id, stem).toLowerCase(), { id: node.id, slot }))
    Object.keys(step.save).forEach(stem => saved.push(outputReference(step.id, stem)))
  }
  for (const ensemble of draft.ensembles) {
    const title = isSimpleAudioOperation(ensemble.algorithm)
      ? t(`workflows.audioOperations.${ensemble.algorithm}.title`)
      : t('workflows.ensembleNode')
    const node = makeNode(`ensemble:${ensemble.id}`, title, ensemble.outputStem, 'process')
    node.inputs = ensemble.inputs.length
    node.outputs = 1
    graph.nodes.push(node)
    sources.set(outputReference(ensemble.id, ensemble.outputStem).toLowerCase(), { id: node.id, slot: 0 })
    if (ensemble.save) saved.push(outputReference(ensemble.id, ensemble.outputStem))
  }
  const connect = (reference: string, target: string, targetSlot: number) => {
    const source = sources.get(reference.trim().toLowerCase())
    if (!source) return
    graph.links.push({ id: String(graph.links.length), source: source.id, sourceSlot: source.slot, target, targetSlot, type: 'AUDIO' })
  }
  draft.steps.forEach(step => connect(step.input, `step:${step.id}`, 0))
  draft.ensembles.forEach(ensemble => ensemble.inputs.forEach((value, slot) => connect(value.source, `ensemble:${ensemble.id}`, slot)))
  if (saved.length) {
    const output = makeNode('save', t('workflows.generatedOutputs'), saved.map(source => source.slice(source.indexOf('.') + 1)).join(' · '), 'output')
    output.inputs = saved.length
    graph.nodes.push(output)
    saved.forEach((source, slot) => connect(source, output.id, slot))
  }
  const positions = new Map<string, { x: number; y: number }>()
  if (isRecord(definition.studio) && isRecord(definition.studio.nodes)) {
    for (const node of graph.nodes) {
      const key = node.id.replace(/^(?:step|ensemble):/, '')
      const point = definition.studio.nodes[key]
      if (isRecord(point) && Number.isFinite(point.x) && Number.isFinite(point.y)) {
        positions.set(node.id, { x: Number(point.x), y: Number(point.y) })
      }
    }
  }
  layoutNodes(graph, positions)
  return graph
}

function buildGraphPreview(definition: Record<string, unknown>, t: Translate): WorkflowPreview {
  const graph: WorkflowPreview = { nodes: [], links: [] }
  const positions = new Map<string, { x: number; y: number }>()
  const nodes = new Map<string, WorkflowPreviewNode>()
  for (const raw of definition.nodes as unknown[]) {
    if (!isRecord(raw) || !['string', 'number'].includes(typeof raw.id) || typeof raw.type !== 'string') continue
    const id = String(raw.id)
    if (nodes.has(id)) continue
    const specType = comfySeparationNodeType(raw.type)
    const spec = Object.hasOwn(NODE_SPECS, specType) ? NODE_SPECS[specType]
      : Object.hasOwn(BUILTIN_SPECS, raw.type) ? BUILTIN_SPECS[raw.type] : undefined
    const title = typeof raw.title === 'string' && raw.title.trim()
      ? raw.title : spec ? translateNodeTitle(spec, t) : raw.type
    const kind: PreviewNodeKind = isWorkflowSaveNodeType(raw.type) ? 'output'
      : isWorkflowSeparationNodeType(raw.type) ? 'separate'
        : raw.type === 'input_audio' || raw.type.startsWith('pymss_load_audio') ? 'input'
          : spec?.category === 'pymss/params' || spec?.category === 'pymss/string' ? 'other' : 'process'
    const widgets = Array.isArray(raw.widgets_values) ? raw.widgets_values : []
    const modelIndex = spec?.widgets.findIndex(widget => widget.name === 'model_name') ?? -1
    const detail = modelIndex >= 0 && typeof widgets[modelIndex] === 'string' ? String(widgets[modelIndex]) : raw.type
    const node = makeNode(id, title, detail, kind)
    if (graphNodeHasCustomParameters(raw.type, isRecord(raw.properties) ? raw.properties.studioInferenceOverrides : undefined, i => widgets[i])) {
      node.customParameters = true
    }
    node.inputs = Array.isArray(raw.inputs) ? raw.inputs.length : 0
    node.outputs = Array.isArray(raw.outputs) ? raw.outputs.length : 0
    const point = raw.pos
    const x = Array.isArray(point) ? point[0] : isRecord(point) ? point['0'] : undefined
    const y = Array.isArray(point) ? point[1] : isRecord(point) ? point['1'] : undefined
    if (Number.isFinite(x) && Number.isFinite(y)) {
      positions.set(id, { x: Number(x), y: Number(y) })
    }
    nodes.set(id, node)
    graph.nodes.push(node)
  }
  const rawLinks = Array.isArray(definition.links) ? definition.links : []
  for (const raw of comfyLinksToLitegraph(rawLinks)) {
    const source = nodes.get(String(raw.origin_id))
    const target = nodes.get(String(raw.target_id))
    if (!source || !target || !Number.isInteger(raw.origin_slot) || raw.origin_slot < 0 || !Number.isInteger(raw.target_slot) || raw.target_slot < 0) continue
    graph.links.push({
      id: String(graph.links.length), source: source.id, target: target.id,
      sourceSlot: raw.origin_slot, targetSlot: raw.target_slot, type: String(raw.type || ''),
    })
    source.outputs = Math.max(source.outputs, raw.origin_slot + 1)
    target.inputs = Math.max(target.inputs, raw.target_slot + 1)
  }
  layoutNodes(graph, positions)
  return graph
}

/** Read-only projection: never configure an editor or rewrite the stored graph. */
export function buildWorkflowPreview(definition: Record<string, unknown>, t: Translate): WorkflowPreview {
  if (isSimpleWorkflowDefinition(definition)) return buildSimplePreview(definition, t)
  if (isGraphWorkflowDefinition(definition)) return buildGraphPreview(definition, t)
  return { nodes: [], links: [] }
}
