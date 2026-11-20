/**
 * Thin adapter between litegraph.serialize() output and the comfy-mss JSON
 * pymss.graph.load_comfy_file expects.
 *
 * litegraph's ISerialisedGraph is already the comfy format (nodes/links with
 * widgets_values). We only normalise a couple of things:
 *  - strip empty/undefined fields pymss does not need
 *  - ensure links are the 6-tuple [id, src, srcSlot, dst, dstSlot, type]
 *  - drop the litegraph-only `floatingLinks` / `reroutes` arrays
 */
import type { ISerialisedGraph, ISerialisedNode, SerialisableGraph, SerialisableLLink } from '@comfyorg/litegraph/dist/types/serialisation'
import type { NativeParameters } from '../features/inference/parameters'
import { comfySeparationNodeType, normalizeGraphWorkflowDefinition } from '../workflows/formats'

export interface ComfyNode {
  id: number
  type: string
  pos: [number, number]
  size: [number, number]
  flags: Record<string, unknown>
  order: number
  mode: number
  inputs?: unknown[]
  outputs?: unknown[]
  properties?: Record<string, unknown>
  widgets_values?: unknown[]
  title?: string
}

export type ComfyLink = [
  number, // link id
  number, // source node id
  number, // source slot
  number, // target node id
  number, // target slot
  string, // type
]

export interface ComfyWorkflow {
  id?: string
  revision?: number
  last_node_id: number
  last_link_id: number
  nodes: ComfyNode[]
  links: ComfyLink[]
  version: number
  groups?: unknown[]
  config?: Record<string, unknown>
  extra?: Record<string, unknown>
}

/**
 * Convert a litegraph-serialized graph into a clean comfy-mss workflow dict
 * suitable for pymss.graph.load_comfy_file (after JSON.stringify).
 */
export function litegraphToComfy(serialized: ISerialisedGraph | any): ComfyWorkflow {
  const rawNodes = normalizeGraphWorkflowDefinition({ nodes: serialized.nodes || [] }).nodes as ISerialisedNode[]
  const nodes: ComfyNode[] = rawNodes.map((n) => {
    const out: ComfyNode = {
      ...n,
      id: Number(n.id),
      type: comfySeparationNodeType(String(n.type)),
      pos: [Number(n.pos?.[0]) || 0, Number(n.pos?.[1]) || 0],
      size: [Number(n.size?.[0]) || 0, Number(n.size?.[1]) || 0],
      flags: (n.flags as Record<string, unknown>) || {},
      order: Number(n.order ?? 0),
      mode: Number(n.mode ?? 0),
    }
    if (n.inputs) out.inputs = n.inputs
    if (n.outputs) out.outputs = n.outputs
    if (n.title) out.title = String(n.title)
    out.properties = n.properties || {}
    // litegraph writes widgets_values only when serialize_widgets is set on the node
    const wv = (n as any).widgets_values
    if (Array.isArray(wv)) out.widgets_values = wv
    return out
  })

  const links: ComfyLink[] = []
  for (const l of comfyLinksToLitegraph(serialized.links || [])) {
    // litegraph serialize() emits object-format links
    // ({id, origin_id, origin_slot, target_id, target_slot, type});
    // convert them into the comfy 6-tuple so pymss/comfy-mss can read them.
    // NOTE: the tuple MUST be a real array — pymss' comfy loader rejects
    // anything that is not isinstance(entry, list); numeric-key objects
    // ({0: id, ...}) serialize to {"0": id} in JSON and get silently dropped.
    const obj = l as Record<string, any>
    if (!obj || typeof obj !== 'object' || obj.id === undefined) continue
    links.push([
      Number(obj.id),
      Number(obj.origin_id),
      Number(obj.origin_slot),
      Number(obj.target_id),
      Number(obj.target_slot),
      String(obj.type ?? ''),
    ])
  }

  const wf: ComfyWorkflow = {
    last_node_id: Number(serialized.state?.lastNodeId ?? serialized.last_node_id ?? (nodes.length ? Math.max(...nodes.map((n) => n.id)) : 0)),
    last_link_id: Number(serialized.state?.lastLinkId ?? serialized.last_link_id ?? (links.length ? Math.max(...links.map((l) => l[0])) : 0)),
    nodes,
    links,
    // ComfyUI schema 1 requires object links and state, not these tuples.
    version: 0.4,
  }
  if (typeof serialized.id === 'string') wf.id = serialized.id
  if (typeof serialized.revision === 'number') wf.revision = serialized.revision
  if (Array.isArray(serialized.groups)) wf.groups = serialized.groups
  if (serialized.config && typeof serialized.config === 'object') wf.config = serialized.config
  if (serialized.extra) wf.extra = serialized.extra
  return wf
}

/** JSON string for pymss.graph.load_comfy_file. */
export function toComfyJson(serialized: ISerialisedGraph | any): string {
  return JSON.stringify(litegraphToComfy(serialized), null, 2)
}

/**
 * Convert comfy-style link tuples ([id, src, srcSlot, dst, dstSlot, type])
 * into the object format litegraph 0.17's LGraph.configure() expects
 * ({ id, origin_id, origin_slot, target_id, target_slot, type }).
 * Without this, configure() sees undefined link ids and collapses every link
 * onto one entry.
 */
export function comfyLinksToLitegraph(links: unknown[]): SerialisableLLink[] {
  const out: SerialisableLLink[] = []
  for (const l of links || []) {
    // litegraph serialize() already emits object-format links
    // ({id, origin_id, ...}) — exactly what configure() wants.
    if (l && typeof l === 'object' && !Array.isArray(l) && (l as any).origin_id !== undefined) {
      out.push(l as SerialisableLLink)
      continue
    }
    // Comfy 6-tuple, either a real array or the numeric-key object
    // ({0: id, 1: src, ...}) produced by litegraphToComfy — JSON round-trips
    // the latter into objects with numeric keys that must be re-read by index.
    const tuple = Array.isArray(l)
      ? l
      : l && typeof l === 'object' && (l as any)[0] !== undefined
        ? [0, 1, 2, 3, 4, 5].map(i => (l as any)[i])
        : null
    if (!tuple || tuple.length < 6) continue
    out.push({
      id: Number(tuple[0]),
      type: String(tuple[5]),
      origin_id: Number(tuple[1]),
      origin_slot: Number(tuple[2]),
      target_id: Number(tuple[3]),
      target_slot: Number(tuple[4]),
    })
  }
  return out
}

/** Overrides travel with the separation node in Comfy JSON and history snapshots. */
export function setGraphNodeInferenceOverrides(node: { properties: Record<string, unknown> }, overrides: NativeParameters) {
  if (Object.keys(overrides).length) node.properties.studioInferenceOverrides = { ...overrides }
  else delete node.properties.studioInferenceOverrides
}

/** Accept native ComfyUI graphs and the tuple/object hybrids saved by older Studio versions. */
export function comfyToLitegraph(definition: Record<string, unknown>): SerialisableGraph & { nodes: ISerialisedNode[] } {
  // Vue definitions can be proxies; LiteGraph structured-clones extra metadata.
  const normalized = normalizeGraphWorkflowDefinition(JSON.parse(JSON.stringify(definition)))
  const nodes = Array.isArray(normalized.nodes) ? normalized.nodes : []
  const links = comfyLinksToLitegraph(Array.isArray(normalized.links) ? normalized.links : [])
  const state = normalized.state && typeof normalized.state === 'object'
    ? normalized.state as Record<string, unknown>
    : {}
  return {
    ...normalized,
    id: (typeof normalized.id === 'string' ? normalized.id : crypto.randomUUID()) as SerialisableGraph['id'],
    revision: typeof normalized.revision === 'number' ? normalized.revision : 0,
    nodes,
    links,
    state: {
      ...state,
      lastNodeId: Math.max(Number(state.lastNodeId || normalized.last_node_id || 0), ...nodes.map(n => Number(n.id) || 0)),
      lastLinkId: Math.max(Number(state.lastLinkId || normalized.last_link_id || 0), ...links.map(l => Number(l.id) || 0)),
      lastGroupId: Number(state.lastGroupId || 0),
      lastRerouteId: Number(state.lastRerouteId || 0),
    },
    version: 1,
  }
}
