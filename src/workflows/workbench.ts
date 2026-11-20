import { detectWorkflowFormat, isWorkflowSaveNodeType } from './formats.ts'
import { explicitOverlapStepIds } from './inferencePolicy.ts'

export type WorkflowNodeState = {
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'skipped' | 'unknown'
  progress: number
  message?: string
  summary?: { completed: number; total: number; failed: number; cancelled: number; skipped: number; unknown: number }
}
export type WorkflowRuntimeBindings = {
  inputs: Record<string, { files: string[]; folder?: string }>
  outputs: Record<string, string>
  outputDirectory?: string
  primaryInput: string
  outputLayout: 'flat' | 'folders'
}
export type WorkflowBindingNode = { id: string; label: string; embeddedPath?: string; subdirectory?: string }
export type WorkflowWorkbenchSnapshot = {
  version: 1
  nativeGraph?: true
  sourceUpdatedAt: number
  sourceDefinition: Record<string, unknown>
  bindings: WorkflowRuntimeBindings
  inputs: Record<string, string>
  outputs: Record<string, string>
}
export type WorkbenchRun = { input: string; inputs: Record<string, string>; outputs: Record<string, string> }

const LOAD_TYPES = new Set(['input_audio', 'pymss_load_audio', 'LoadAudio', 'pymss_load_audio_batch'])
function record(value: unknown): value is Record<string, any> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}
export function cloneWorkflow<T>(value: T): T { return JSON.parse(JSON.stringify(value)) as T }
export function createWorkflowBindings(): WorkflowRuntimeBindings {
  return { inputs: {}, outputs: {}, primaryInput: '', outputLayout: 'flat' }
}
export function workflowInputNodes(definition: Record<string, unknown>): WorkflowBindingNode[] {
  if (detectWorkflowFormat(definition) === 'simple') return [{ id: 'input', label: 'Input' }]
  return (Array.isArray(definition.nodes) ? definition.nodes : []).filter(record)
    .filter(node => LOAD_TYPES.has(String(node.type)))
    .map(node => {
      const widgets = Array.isArray(node.widgets_values) ? node.widgets_values : []
      const path = String(widgets[0] || '')
      return {
        id: String(node.id), label: String(node.title || `${node.type} #${node.id}`),
        ...(String(node.type) !== 'pymss_load_audio_batch' && /[\\/]/.test(path) ? { embeddedPath: path } : {}),
      }
    })
}
export function workflowOutputNodes(definition: Record<string, unknown>): WorkflowBindingNode[] {
  if (detectWorkflowFormat(definition) !== 'simple') {
    return (Array.isArray(definition.nodes) ? definition.nodes : []).filter(record)
      .filter(node => isWorkflowSaveNodeType(node.type))
      .map(node => {
        const widgets = Array.isArray(node.widgets_values) ? node.widgets_values : []
        const dynamicPrefix = (Array.isArray(node.inputs) ? node.inputs : []).some((input: any) => input?.name === 'filename_prefix' && input.link != null)
        const folder = node.type === 'pymss_save_audio' ? String(widgets[1] || '')
          : dynamicPrefix ? '' : String(widgets[0] || '').replace(/\\/g, '/').split('/').slice(0, -1).join('/')
        return { id: String(node.id), label: String(node.title || `${node.type} #${node.id}`),
          ...(!folder || /^\d+$/.test(folder) || folder.toLowerCase() === 'default' ? {} : { subdirectory: safeDirectoryPart(folder) }) }
      })
  }
  const nodes: WorkflowBindingNode[] = []
  for (const step of (Array.isArray(definition.steps) ? definition.steps : []).filter(record)) {
    for (const [stem, target] of Object.entries(record(step.save) ? step.save : {})) {
      if (target && (!Array.isArray(target) || target.some(Boolean))) {
        nodes.push({ id: `save:${step.id}:${stem}`, label: `${step.id} · ${stem}` })
      }
    }
  }
  for (const item of (Array.isArray(definition.ensembles) ? definition.ensembles : []).filter(record)) {
    if (item.save) nodes.push({ id: `studio:ensemble-save:${item.id}`, label: `${item.id} · ${item.output_stem}` })
  }
  return nodes
}
function fileStem(path: string, windows: boolean): string {
  const stem = (path.split(windows ? /[\\/]/ : /[/]/).pop() || 'workflow').replace(/\.[^.]+$/, '')
  return windows ? safeDirectoryPart(stem) : !stem || stem === '.' || stem === '..' ? 'workflow' : stem
}
function safeDirectoryPart(value: string): string {
  return value.replace(/[<>:"/\\|?*\x00-\x1f]+/g, '_').replace(/^[. ]+|[. ]+$/g, '') || 'workflow'
}
function appendDirectory(root: string, child: string, windows: boolean): string {
  return `${root.replace(windows ? /[\\/]+$/ : /[/]+$/, '')}${windows && /^[A-Za-z]:|^\\\\/.test(root) ? '\\' : '/'}${child}`
}
export function workbenchOutputDirectory(node: WorkflowBindingNode, override: string | undefined, defaultDirectory: string, windows = /^[A-Za-z]:|^\\\\/.test(override || defaultDirectory)): string {
  const root = override || defaultDirectory
  return node.subdirectory ? appendDirectory(root, node.subdirectory, windows) : root
}

/** Expand simple workflow bindings per file; execute native graphs unchanged once. */
export function prepareWorkbenchRun(
  definition: Record<string, unknown>, bindings: WorkflowRuntimeBindings, defaultDirectory: string,
  windows = /^[A-Za-z]:|^\\\\/.test(bindings.outputDirectory || defaultDirectory),
): { definition: Record<string, unknown>; runs: WorkbenchRun[] } {
  const transient = cloneWorkflow(definition)
  if (detectWorkflowFormat(transient) === 'graph') {
    if (!(bindings.outputDirectory?.trim() || defaultDirectory.trim())) throw new Error('workbench.missingOutputDirectory')
    return { definition: transient, runs: [{ input: '', inputs: {}, outputs: {} }] }
  }
  const inputNodes = workflowInputNodes(transient)
  const outputNodes = workflowOutputNodes(transient)
  if (!outputNodes.length) throw new Error('workbench.noOutputs')
  const filesByNode = new Map(inputNodes.map(node => [node.id, [
    ...(bindings.inputs[node.id]?.files || (node.embeddedPath ? [node.embeddedPath] : [])),
  ].filter(Boolean)]))
  if (inputNodes.some(node => !filesByNode.get(node.id)?.length)) throw new Error('workbench.missingInput')
  const primary = inputNodes.find(node => node.id === bindings.primaryInput) || inputNodes[0]
  const multiple = inputNodes.filter(node => (filesByNode.get(node.id)?.length || 0) > 1)
  if (multiple.length > 1) throw new Error('workbench.multipleBatches')
  if (multiple.length && multiple[0].id !== primary?.id) throw new Error('workbench.primaryBatch')
  const sharedDirectory = bindings.outputDirectory
  if (outputNodes.some(node => !(sharedDirectory || bindings.outputs[node.id] || defaultDirectory).trim())) {
    throw new Error('workbench.missingOutputDirectory')
  }
  const files = primary ? filesByNode.get(primary.id)! : ['']
  return {
    definition: transient,
    runs: files.map(input => {
      const outputs = Object.fromEntries(outputNodes.map(node => {
        const root = workbenchOutputDirectory(node, sharedDirectory || bindings.outputs[node.id], defaultDirectory, windows)
        return [node.id, bindings.outputLayout === 'folders' ? appendDirectory(root, fileStem(input, windows), windows) : root]
      }))
      return { input, inputs: {}, outputs }
    }),
  }
}

/** Group compiler-generated nodes into the visible simple editor nodes. */
export function visibleWorkflowNodeId(runtimeId: string, definition: Record<string, unknown>): string {
  if (detectWorkflowFormat(definition) !== 'simple') return runtimeId
  if (runtimeId === 'input' || runtimeId === 'input_audio') return 'input'
  const steps = (Array.isArray(definition.steps) ? definition.steps : []).filter(record)
  const step = steps.find(item => typeof item.id === 'string' && runtimeId === `step:${item.id.trim()}`)
  if (step) return String(step.id)
  if (runtimeId.startsWith('save:') || runtimeId.startsWith('studio:ensemble-save:')) return 'save'
  const items = (Array.isArray(definition.ensembles) ? definition.ensembles : []).filter(record)
  const ensemble = items.find(item => runtimeId === `studio:ensemble:${item.id}` || runtimeId.startsWith(`studio:ensemble:${item.id}:mix:`))
  return ensemble ? String(ensemble.id) : ''
}

export const WORKFLOW_NODE_STATUS_KEYS: Record<string, string> = {
  queued: 'tasks.statusQueued', skipped: 'workbench.notExecuted', unknown: 'workbench.noNodeRecord',
  running: 'workbench.running', done: 'workbench.done', failed: 'workbench.failed', cancelled: 'workbench.cancelled',
}

export function visibleWorkflowNodeStates(definition: Record<string, unknown>, states: Record<string, WorkflowNodeState> = {}): Record<string, WorkflowNodeState> {
  const result: Record<string, WorkflowNodeState> = {}
  for (const [id, state] of Object.entries(states)) {
    const visible = visibleWorkflowNodeId(id, definition)
    if (!visible) continue
    result[visible] = id.includes(':mix:') && state.status === 'done' ? { ...state, status: 'running' } : state
  }
  if (detectWorkflowFormat(definition) === 'simple' && result.save) {
    const outputs = workflowOutputNodes(definition).map(node => states[node.id])
    result.save = {
      status: outputs.some(state => state?.status === 'failed') ? 'failed'
        : outputs.some(state => state?.status === 'cancelled') ? 'cancelled'
        : outputs.length && outputs.every(state => state?.status === 'done') ? 'done' : 'running',
      progress: outputs.reduce((sum, state) => sum + (state?.progress || 0), 0) / Math.max(1, outputs.length),
    }
  }
  return result
}

/** Compare processing data while ignoring camera positions and node appearance. */
export function workflowExecutionSignature(definition: Record<string, unknown>): string {
  const data = cloneWorkflow(definition)
  if (detectWorkflowFormat(data) === 'simple') {
    const nativeSteps = explicitOverlapStepIds(data.studio)
    if (nativeSteps.size) data.studio = { nativeInferenceSteps: [...nativeSteps].sort() }
    else delete data.studio
  }
  else {
    for (const key of ['last_node_id', 'last_link_id', 'groups']) delete data[key]
    for (const node of (Array.isArray(data.nodes) ? data.nodes : []).filter(record)) {
      for (const key of ['pos', 'size', 'color', 'bgcolor', 'boxcolor', 'shape', 'order']) delete node[key]
      if (record(node.flags)) {
        delete node.flags.collapsed
        if (!Object.keys(node.flags).length) delete node.flags
      }
    }
    if (Array.isArray(data.nodes)) data.nodes.sort((a, b) => String(a.id).localeCompare(String(b.id)))
    if (Array.isArray(data.links)) data.links.sort((a, b) => String(Array.isArray(a) ? a[0] : a.id).localeCompare(String(Array.isArray(b) ? b[0] : b.id)))
    if (record(data.extra)) {
      for (const key of ['ds', 'viewport', 'studioViewport']) delete data.extra[key]
      if (!Object.keys(data.extra).length) delete data.extra
    }
  }
  return JSON.stringify(canonical(data))
}

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical)
  if (!record(value)) return value
  return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]))
}

export function workflowBindingsEqual(first: WorkflowRuntimeBindings, second: WorkflowRuntimeBindings): boolean {
  return JSON.stringify(canonical(first)) === JSON.stringify(canonical(second))
}
