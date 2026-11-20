import { litegraphToComfy, type ComfyWorkflow } from '../litegraph/graphAdapter'
import { NODE_SPECS } from '../litegraph/nodeSpecs'
import { workflowParameterTargets, type WorkflowNodeParameters, type WorkflowParameterPlan } from './inferencePolicy'

export interface ComfyExportParameterPlan extends WorkflowParameterPlan {
  nodes: Record<string, WorkflowNodeParameters & { modelDefaultOverlap?: number }>
}

export class ComfyParameterExportError extends Error {
  constructor(readonly node: string, readonly field: string) {
    super(`${node} · ${field}`)
  }
}

export function buildComfyWorkflowExport(definition: Record<string, unknown>, plan?: ComfyExportParameterPlan): ComfyWorkflow {
  const graph = litegraphToComfy(JSON.parse(JSON.stringify(definition)))
  const targets = new Map(workflowParameterTargets(definition).map(target => [target.id, target]))
  let nodeId = Math.max(graph.last_node_id, ...graph.nodes.map(node => node.id))
  let linkId = Math.max(graph.last_link_id, ...graph.links.map(link => link[0]))
  let order = Math.max(0, ...graph.nodes.map(node => node.order))
  for (const node of graph.nodes.slice()) {
    const target = targets.get(String(node.id))
    if (!target) continue
    const widgets = node.widgets_values
    if (target.kind !== 'custom' && widgets?.[3] === 'Default') widgets[3] = 'modelscope'
    if (!Object.keys(target.overrides).length) continue
    const label = target.label || target.id
    const resolved = plan?.nodes[target.id]
    if (!resolved || target.dynamic) throw new ComfyParameterExportError(label, 'params')

    const type = target.kind === 'vr' ? 'pymss_vr_params' : 'pymss_mss_params'
    const spec = NODE_SPECS[type]!
    const port = spec.outputs[0]!
    const values: Record<string, number | boolean> = { ...resolved.parameters, enable_tta: resolved.useTta }
    for (const key of Object.keys(target.overrides)) {
      if (!spec.widgets.some(widget => widget.name === key) || resolved.sources[key] !== 'node') {
        throw new ComfyParameterExportError(label, key)
      }
    }
    const parameterWidgets = spec.widgets.map(widget => {
      const value = values[widget.name]
      switch (widget.name) {
        case 'chunk_size':
          if (value === undefined || resolved.sources.chunk_size === 'model-config') return 'Default'
          if (typeof value === 'number' && Number.isSafeInteger(value) && value > 0) return String(value)
          break
        case 'overlap_size':
          if (value === undefined || (resolved.sources.overlap_size === 'model-config'
            && (resolved.sources.chunk_size === 'model-config' || value === 0 && resolved.modelDefaultOverlap === 0))) return 'Default'
          if (typeof value === 'number' && Number.isSafeInteger(value) && value > 0) return String(value)
          break
        case 'window_size':
          if (typeof value === 'number' && value >= 128 && value <= 4096 && value % 128 === 0) return value
          break
        case 'batch_size':
          if (typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= 128) return value
          break
        default:
          if (widget.type === 'toggle' && typeof value === 'boolean') return value
          if (widget.type === 'number' && typeof value === 'number' && Number.isFinite(value)
            && value >= (widget.min ?? -Infinity) && value <= (widget.max ?? Infinity)) return value
      }
      throw new ComfyParameterExportError(label, widget.name)
    })

    const inputs = (node.inputs ??= []) as Array<{ name: string; type: string; link?: number | null; shape?: number }>
    let slot = inputs.findIndex(input => input.name === 'params')
    if (slot < 0) {
      if (inputs.length !== 1 || inputs[0]?.name !== 'audio') throw new ComfyParameterExportError(label, 'params')
      slot = inputs.length
      inputs.push({ name: 'params', type: port.type, shape: 7 })
    }
    const input = inputs[slot]!
    if (input.link != null) {
      const previous = graph.links.findIndex(link => link[0] === input.link)
      if (previous < 0) throw new ComfyParameterExportError(label, 'params')
      const [, origin, originSlot] = graph.links[previous]!
      const producer = graph.nodes.find(item => item.id === origin)
      const output = producer?.outputs?.[originSlot] as { links?: number[] } | undefined
      if (Array.isArray(output?.links)) output.links = output.links.filter(id => id !== input.link)
      graph.links.splice(previous, 1)
    }

    // Use a separate parameter node so existing links to shared producers remain unchanged.
    graph.nodes.push({
      id: ++nodeId, type, pos: [node.pos[0] - 240, node.pos[1] + 100],
      size: [220, 220], flags: {}, order: ++order, mode: 0, inputs: [],
      outputs: [{ name: port.name, type: port.type, links: [++linkId] }],
      properties: {}, widgets_values: parameterWidgets,
    })
    input.link = linkId
    graph.links.push([linkId, nodeId, 0, node.id, slot, port.type])
    delete node.properties?.studioInferenceOverrides
  }

  const linkedProducers = new Set(graph.links.map(link => link[1]))
  const zeroSizes = new Set(['0', '0.0'])
  for (const node of graph.nodes) {
    if (node.type !== 'pymss_mss_params' || !linkedProducers.has(node.id) || !node.widgets_values) continue
    const widgets = node.widgets_values
    if (zeroSizes.has(String(widgets[1]).trim())) {
      throw new ComfyParameterExportError(node.title || `#${node.id}`, 'overlap_size')
    }
    if (zeroSizes.has(String(widgets[2]).trim())) widgets[2] = 'Default'
  }
  graph.last_node_id = nodeId
  graph.last_link_id = linkId
  return graph
}
