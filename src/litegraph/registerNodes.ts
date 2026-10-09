/**
 * Register all pymss comfy-mss node types into LiteGraph.
 *
 * Each node's `type` string equals the comfy class_type pymss registers
 * (pymss/graph/nodes.py), so a serialized graph loads directly via
 * pymss.graph.load_comfy_file.
 */
import { LiteGraph, LGraphNode, LGraphCanvas, type LGraphNode as LGraphNodeType } from '@comfyorg/litegraph'
import { NODE_SPECS, BUILTIN_SPECS, NodeSpec, WidgetSpec, PORT } from './nodeSpecs'
import { isWorkflowSeparationNodeType } from '../workflows/formats'
import {
  setNodeTranslator,
  translateAdvancedEditorText,
  translateLiteGraphMenuText,
  translateNodeCategory,
  translateNodeField,
  translateNodeTitle,
  type NodeTranslator,
} from './nodeLocalization'

type AnyNode = any

const DEFAULT_TITLE_FLAG = '__pymssUsesDefaultTitle'
const LAST_DEFAULT_TITLE = '__pymssLastDefaultTitle'
const ORIGINAL_LOCALIZED_NAME = '__pymssOriginalLocalizedName'
const ORIGINAL_LABEL = '__pymssOriginalLabel'
const LAST_LOCALIZED_LABEL = '__pymssLastLocalizedLabel'
const LOCALIZE_LABEL_FLAG = '__pymssLocalizeLabel'

export const SEPARATE_NODE_MIN_WIDTH = 300

export const PORT_COLORS: Record<string, string> = {
  [PORT.AUDIO]: '#66d19e',
  [PORT.STRING]: '#f2c94c',
  [PORT.MSS_PARAMS]: '#e879f9',
  [PORT.VR_PARAMS]: '#a78bfa',
  [PORT.BOOLEAN]: '#60a5fa',
  [PORT.COMBO]: '#94a3b8',
}

const PORT_COLORS_OFF: Record<string, string> = {
  [PORT.AUDIO]: '#39755c',
  [PORT.STRING]: '#806d32',
  [PORT.MSS_PARAMS]: '#74427c',
  [PORT.VR_PARAMS]: '#594a83',
  [PORT.BOOLEAN]: '#3c6091',
  [PORT.COMBO]: '#526174',
}

export function applyPymssPortColors(canvas?: LGraphCanvas) {
  Object.assign(LGraphCanvas.link_type_colors, PORT_COLORS)
  if (!canvas) return
  Object.assign(canvas.default_connection_color_byType, PORT_COLORS)
  Object.assign(canvas.default_connection_color_byTypeOff, PORT_COLORS_OFF)
}

function localizeContextMenu(menu: any) {
  const root = menu?.root as HTMLElement | undefined
  if (!root) return
  for (const child of Array.from(root.children) as HTMLElement[]) {
    if (!child.classList.contains('litemenu-entry') && !child.classList.contains('litemenu-title')) continue
    const source = child.textContent?.trim() || ''
    const translated = translateLiteGraphMenuText(source)
    if (translated !== source) child.textContent = translated
  }
}

// 右键菜单/搜索框的子菜单默认需要点击才展开。litegraph 的 ContextMenu 支持
// autoopen 选项(hover 自动展开子菜单)但没有全局开关,这里 patch 构造函数,
// 所有菜单默认 autoopen: true。
if (typeof window !== 'undefined') {
  const CM = (LiteGraph as any).ContextMenu
  if (CM && !CM.__pymssAutoopenPatched) {
    const origCtor = CM
    const PatchedCM = function (this: any, ...args: any[]) {
      if (args[1] && typeof args[1] === 'object') args[1].autoopen = args[1].autoopen ?? true
      const menu = new origCtor(...args)
      // LiteGraph opens nested menus through `that.constructor`. The object
      // returned above is an origCtor instance, so point its constructor back
      // to this wrapper to keep auto-open and localization active recursively.
      Object.defineProperty(menu, 'constructor', {
        configurable: true,
        value: PatchedCM,
        writable: true,
      })
      localizeContextMenu(menu)
      return menu
    }
    PatchedCM.prototype = origCtor.prototype
    Object.setPrototypeOf(PatchedCM, origCtor)
    for (const k of Object.getOwnPropertyNames(origCtor)) {
      if (!['prototype', 'name', 'length'].includes(k)) {
        try { (PatchedCM as any)[k] = (origCtor as any)[k] } catch { /* getter-only */ }
      }
    }
    ;(LiteGraph as any).ContextMenu = PatchedCM
    PatchedCM.__pymssAutoopenPatched = true
  }
}

function widgetCallback(node: AnyNode, spec: WidgetSpec) {
  return (v: any) => {
    node.properties[spec.name] = v
    if (spec.name === 'model_name') {
      node.onModelNameChanged?.(v)
    }
    if (node.type === 'pymss_audio_ensemble' && spec.name === 'input_count') {
      syncEnsembleInputs(node, v)
    }
    // Widget edits (changeValue path) do not notify the graph on their own —
    // canvas node ops wrap themselves in beforeChange/afterChange, but
    // widget edits don't. Fire afterChange so undo/save toolbars enable.
    node.graph?.afterChange(node)
  }
}

function syncEnsembleInputs(node: AnyNode, value: unknown) {
  const count = Math.max(2, Math.min(10, Math.trunc(Number(value)) || 2))
  // Only remove trailing audio slots; retained input indices and links stay intact.
  for (let i = node.inputs.length - 1; i >= 0; i--) {
    const match = /^audio_(\d+)$/.exec(node.inputs[i].name)
    if (match && Number(match[1]) > count) node.removeInput(i)
  }
  for (let i = 1; i <= count; i++) {
    if (!node.inputs.some((slot: any) => slot.name === `audio_${i}`)) {
      node.addInput(`audio_${i}`, PORT.AUDIO, { shape: 7 })
    }
  }
  for (const widget of node.widgets || []) {
    const match = /^weight_(\d+)$/.exec(widget.name)
    if (!match) continue
    widget.hidden = Number(match[1]) > count
    // LiteGraph 0.17 still allocates layout space for hidden widgets.
    widget.computeLayoutSize = widget.hidden ? () => ({ minHeight: 0, maxHeight: 0, minWidth: 0 }) : undefined
  }
  node.setSize([node.size[0], node.computeSize()[1]])
  localizePymssNode(node)
}

function createWidgets(node: AnyNode, specs: WidgetSpec[]) {
  node.widgets = []
  for (const spec of specs) {
    node.properties[spec.name] = spec.default
    const widget = node.addWidget(
      spec.type,
      spec.name,
      spec.default,
      widgetCallback(node, spec),
      spec.options ? { values: spec.options } : undefined,
    )
    if (widget) widget.value = spec.default
  }
}

function normalizeBuiltinWidgetLayout(spec: NodeSpec, info: any) {
  const values = Array.isArray(info?.widgets_values) ? info.widgets_values : []
  let next: unknown[] | null = null

  if (spec.type === 'SaveAudio' && values.length === 0) {
    next = ['audio']
  } else if (spec.type === 'SaveAudioMP3' && values.length === 1) {
    next = ['audio', values[0]]
  } else if (spec.type === 'SaveAudioOpus' && values.length === 1) {
    next = ['audio', values[0]]
  } else if (spec.type === 'SaveAudioAdvanced' && values.length >= 5) {
    const format = String(values[0] || 'flac').toLowerCase()
    const quality = format === 'mp3'
      ? values[4] || '320k'
      : format === 'opus'
        ? '128k'
        : ''
    next = ['audio', format, quality]
  } else if (spec.type === 'AudioConcat' && values.length === 1) {
    const direction = String(values[0] || '').toLowerCase()
    if (direction === 'front' || direction === 'back') {
      next = [direction === 'front' ? 'before' : 'after']
    }
  } else if (spec.type === 'StringTrim' && values.length === 1) {
    const modes: Record<string, string> = { both: 'Both', left: 'Left', right: 'Right' }
    next = ['', modes[String(values[0] || '').toLowerCase()] || 'Both']
  } else if (spec.type === 'CaseConverter' && values.length === 1) {
    const modes: Record<string, string> = {
      upper: 'UPPERCASE',
      lower: 'lowercase',
      title: 'Title Case',
      capitalize: 'Capitalize',
    }
    next = ['', modes[String(values[0] || '').toLowerCase()] || 'UPPERCASE']
  } else if (spec.type === 'RegexExtract' && values.length === 1) {
    const modes: Record<string, string> = { first: 'First Match', all: 'All Matches' }
    next = ['', '', modes[String(values[0] || '').toLowerCase()] || 'First Match', 1]
  }

  let normalized = next ? { ...info, widgets_values: next } : info
  if (spec.type === 'SaveAudioAdvanced'
    && (!Array.isArray(normalized?.outputs) || normalized.outputs.length === 0)) {
    normalized = {
      ...normalized,
      outputs: spec.outputs.map(output => ({ ...output, links: null })),
    }
  }
  return normalized
}

if (typeof window !== 'undefined') {
  const proto = (LGraphCanvas as any)?.prototype
  if (proto && !proto.__pymssI18nPatched) {
    const originalSearch = proto.showSearchBox
    proto.showSearchBox = function (this: any, ...args: any[]) {
      const dialog = originalSearch.apply(this, args)
      const title = dialog?.querySelector?.('.name') as HTMLElement | null
      const input = dialog?.querySelector?.('input') as HTMLInputElement | null
      if (title) title.textContent = translateAdvancedEditorText('searchNodes', 'Search nodes')
      if (input) input.placeholder = translateAdvancedEditorText('searchNodes', 'Search nodes')
      return dialog
    }
    const originalPrompt = proto.prompt
    proto.prompt = function (this: any, title: string, ...args: any[]) {
      const translatedTitle = title === 'Value'
        ? translateAdvancedEditorText('value', title)
        : title
      const result = originalPrompt.call(this, translatedTitle, ...args)
      const button = this.prompt_box?.querySelector?.('button') as HTMLButtonElement | null
      if (button) button.textContent = translateAdvancedEditorText('confirm', 'OK')
      return result
    }
    proto.__pymssI18nPatched = true
  }
}

/** Default stems when no model is selected yet (two placeholder slots). */
const DEFAULT_STEMS = ['stem_1', 'stem_2']

/** Build a stem pair of outputs: `<stem> (Audio)` + `<stem> (String)`. */
function stemOutputNames(stem: string) {
  return [`${stem} (Audio)`, `${stem} (String)`]
}

function outputIdentity(name: unknown) {
  const match = /^(.*) \((Audio|String)\)$/.exec(String(name || '').trim())
  if (!match) return null
  return {
    stem: match[1].trim(),
    kind: match[2].toLowerCase(),
  }
}

function graphLink(graph: any, linkId: unknown) {
  if (!graph || linkId == null) return null
  if (graph.links instanceof Map) return graph.links.get(linkId) || null
  if (graph._links instanceof Map) return graph._links.get(linkId) || null
  return graph.links?.[linkId as any] || graph._links?.[linkId as any] || null
}

type RerouteSnapshot = {
  id: unknown
  parentId: unknown
  pos: [number, number]
  floating: unknown
}

function captureRerouteChain(graph: any, parentId: unknown, snapshots: Map<unknown, RerouteSnapshot>) {
  const visited = new Set<unknown>()
  let currentId: unknown = parentId
  while (currentId != null && !visited.has(currentId)) {
    visited.add(currentId)
    const reroute = graph?.getReroute?.(currentId) || graph?.reroutes?.get?.(currentId)
    if (!reroute) break
    snapshots.set(currentId, {
      id: currentId,
      parentId: reroute.parentId,
      pos: [Number(reroute.pos?.[0] || 0), Number(reroute.pos?.[1] || 0)],
      floating: reroute.floating,
    })
    currentId = reroute.parentId
  }
}

function restoreRerouteChain(graph: any, parentId: unknown, snapshots: Map<unknown, RerouteSnapshot>) {
  if (parentId == null || !graph?.setReroute) return undefined
  const chain: RerouteSnapshot[] = []
  const visited = new Set<unknown>()
  let currentId: unknown = parentId
  while (currentId != null && !visited.has(currentId)) {
    visited.add(currentId)
    const snapshot = snapshots.get(currentId)
    if (!snapshot) break
    chain.push(snapshot)
    currentId = snapshot.parentId
  }
  for (const snapshot of chain.reverse()) {
    if (graph.getReroute?.(snapshot.id) || graph.reroutes?.get?.(snapshot.id)) continue
    graph.setReroute({
      id: snapshot.id,
      parentId: snapshot.parentId,
      pos: snapshot.pos,
      linkIds: [],
      floating: snapshot.floating,
    })
  }
  return graph.getReroute?.(parentId) || graph.reroutes?.get?.(parentId)
    ? parentId
    : undefined
}

/**
 * Apply the dynamic stem outputs to a separate node (idempotent).
 * Called by the editor after the user picks a model whose stems are known.
 */
export function setSeparateStems(node: LGraphNodeType, stems: string[]) {
  const n = node as AnyNode
  const list = stems.length ? stems : DEFAULT_STEMS
  const desiredNames = list.flatMap(stemOutputNames)
  const currentNames = ((n.outputs || []) as Array<{ name?: unknown }>).map(output => String(output.name || ''))
  if (
    currentNames.length === desiredNames.length
    && currentNames.every((name: string, index: number) => name === desiredNames[index])
  ) {
    n.stems = list
    n.setSize([Math.max(n.size[0], SEPARATE_NODE_MIN_WIDTH), n.computeSize()[1]])
    localizePymssNode(n)
    return
  }

  const preservedLinks: Array<{
    stem: string
    kind: string
    placeholderIndex: number | null
    targetNode: AnyNode
    targetSlot: number
    parentRerouteId: unknown
  }> = []
  const graph = n.graph as any
  const rerouteSnapshots = new Map<unknown, RerouteSnapshot>()
  for (const output of n.outputs || []) {
    const identity = outputIdentity(output.name)
    if (!identity) continue
    const placeholder = /^stem_(\d+)$/i.exec(identity.stem)
    for (const linkId of output.links || []) {
      const link = graphLink(graph, linkId)
      const targetNode = link ? graph?.getNodeById?.(link.target_id) as AnyNode | undefined : undefined
      if (!link || !targetNode) continue
      captureRerouteChain(graph, link.parentId, rerouteSnapshots)
      preservedLinks.push({
        stem: identity.stem.toLowerCase(),
        kind: identity.kind,
        placeholderIndex: placeholder ? Math.max(0, Number(placeholder[1]) - 1) : null,
        targetNode,
        targetSlot: Number(link.target_slot),
        parentRerouteId: link.parentId,
      })
    }
  }

  while (n.outputs && n.outputs.length) n.removeOutput(0)
  for (const stem of list) {
    for (const name of stemOutputNames(stem)) {
      n.addOutput(name, name.endsWith('(String)') ? PORT.STRING : PORT.AUDIO)
    }
  }

  const outputSlots = new Map<string, number>()
  for (const [index, output] of (n.outputs || []).entries()) {
    const identity = outputIdentity(output.name)
    if (identity) outputSlots.set(`${identity.stem.toLowerCase()}\0${identity.kind}`, index)
  }
  for (const link of preservedLinks) {
    const fallbackSlot = link.placeholderIndex === null
      ? undefined
      : link.placeholderIndex * 2 + (link.kind === 'string' ? 1 : 0)
    const outputSlot = outputSlots.get(`${link.stem}\0${link.kind}`) ?? fallbackSlot
    if (outputSlot === undefined || outputSlot < 0 || outputSlot >= (n.outputs?.length || 0)) continue
    try {
      const parentRerouteId = restoreRerouteChain(graph, link.parentRerouteId, rerouteSnapshots)
      n.connect(outputSlot, link.targetNode, link.targetSlot, parentRerouteId)
    } catch {
      // LiteGraph rejects connections whose target disappeared during the
      // same edit; incompatible links are intentionally left disconnected.
    }
  }
  n.stems = list
  n.setSize([Math.max(n.size[0], SEPARATE_NODE_MIN_WIDTH), n.computeSize()[1]])
  localizePymssNode(n)
}

/** Inject the downloaded-model list into every separate node's model_name
 * combo: spec defaults (new nodes) + live widgets (editor calls refresh below). */
export function applyModelOptions(values: string[]) {
  for (const spec of Object.values(NODE_SPECS)) {
    for (const w of spec.widgets) {
      if (w.name === 'model_name') w.options = values
    }
  }
}

/** Refresh one node instance's model_name combo options (call after graph
 * configure and whenever the model list changes). */
export function refreshNodeModelOptions(node: any, values: string[]) {
  const widget = (node.widgets || []).find((w: any) => w.name === 'model_name')
  if (!widget) return
  widget.options = widget.options || {}
  widget.options.values = values
}

function specForNode(node: AnyNode): NodeSpec | undefined {
  const type = String(node?.type || '')
  return NODE_SPECS[type]
    || BUILTIN_SPECS[type]
    || NODE_SPECS[type.replace(/^pymss_/, '')]
}

function captureOriginalSlotMetadata(slot: AnyNode) {
  if (Object.prototype.hasOwnProperty.call(slot, ORIGINAL_LOCALIZED_NAME)) return
  const metadata = {
    [ORIGINAL_LOCALIZED_NAME]: slot.localized_name,
    [ORIGINAL_LABEL]: slot.label,
    [LAST_LOCALIZED_LABEL]: undefined,
    [LOCALIZE_LABEL_FLAG]: slot.label === undefined
      || slot.label === slot.name
      || slot.label === slot.localized_name,
  }
  for (const [key, value] of Object.entries(metadata)) {
    Object.defineProperty(slot, key, { configurable: true, value, writable: true })
  }
}

function localizeSlots(slots: AnyNode[] | undefined, translator?: NodeTranslator) {
  for (const slot of slots || []) {
    captureOriginalSlotMetadata(slot)
    const translated = translateNodeField(String(slot.name || ''), translator)
    if (slot[LOCALIZE_LABEL_FLAG]) {
      const last = slot[LAST_LOCALIZED_LABEL]
      if (last !== undefined && slot.label !== last && slot.label !== slot[ORIGINAL_LABEL]) {
        slot[LOCALIZE_LABEL_FLAG] = false
      } else {
        slot.label = translated
        slot[LAST_LOCALIZED_LABEL] = translated
      }
    }
    slot.localized_name = translated
  }
}

function usesDefaultTitle(node: AnyNode, spec: NodeSpec) {
  if (node[DEFAULT_TITLE_FLAG] === false) return false
  const current = String(node.title || '')
  const previous = String(node[LAST_DEFAULT_TITLE] || '')
  if (current && current !== spec.title && current !== previous) {
    node[DEFAULT_TITLE_FLAG] = false
    return false
  }
  return true
}

function localizeNodeWithSpec(node: AnyNode, spec: NodeSpec, translator?: NodeTranslator) {
  if (usesDefaultTitle(node, spec)) {
    const title = translateNodeTitle(spec, translator)
    node.title = title
    node[DEFAULT_TITLE_FLAG] = true
    node[LAST_DEFAULT_TITLE] = title
  }
  for (const widget of node.widgets || []) {
    widget.label = translateNodeField(String(widget.name || ''), translator)
  }
  localizeSlots(node.inputs, translator)
  localizeSlots(node.outputs, translator)
}

export function localizePymssNode(node: AnyNode, translator?: NodeTranslator) {
  const spec = specForNode(node)
  if (spec) localizeNodeWithSpec(node, spec, translator)
}

function restoreSerializedLocalizedNames(serialized: AnyNode[] | undefined, runtime: AnyNode[] | undefined) {
  for (let index = 0; index < (serialized?.length || 0); index++) {
    const slot = runtime?.[index]
    const originalLocalizedName = slot?.[ORIGINAL_LOCALIZED_NAME]
    if (originalLocalizedName === undefined) delete serialized![index].localized_name
    else serialized![index].localized_name = originalLocalizedName

    if (slot?.[LOCALIZE_LABEL_FLAG]
      && slot[LAST_LOCALIZED_LABEL] !== undefined
      && slot.label !== slot[LAST_LOCALIZED_LABEL]
      && slot.label !== slot[ORIGINAL_LABEL]) {
      slot[LOCALIZE_LABEL_FLAG] = false
    }
    if (slot?.[LOCALIZE_LABEL_FLAG]) {
      if (slot[ORIGINAL_LABEL] === undefined) delete serialized![index].label
      else serialized![index].label = slot[ORIGINAL_LABEL]
    }
  }
}

const registeredNodeClasses: { cls: AnyNode; spec: NodeSpec }[] = []

function refreshRegisteredNodeMetadata(translator?: NodeTranslator) {
  for (const { cls, spec } of registeredNodeClasses) {
    cls.title = translateNodeTitle(spec, translator)
    cls.category = translateNodeCategory(spec.category, translator)
  }
}

export function setPymssNodeTranslator(translator?: NodeTranslator) {
  setNodeTranslator(translator)
  refreshRegisteredNodeMetadata(translator)
}

function makeNodeClass(spec: NodeSpec): any {
  const klass = class extends LGraphNode {
    static title = translateNodeTitle(spec)
    static category = translateNodeCategory(spec.category)
    stems: string[] = spec.dynamicStems ? DEFAULT_STEMS : []

    constructor(title?: string) {
      super(title || translateNodeTitle(spec))
      ;(this as AnyNode)[DEFAULT_TITLE_FLAG] = !title
        || title === spec.title
        || title === translateNodeTitle(spec)
      ;(this as AnyNode)[LAST_DEFAULT_TITLE] = this.title
      this.serialize_widgets = true
      this.properties = this.properties || {}
      createWidgets(this, spec.widgets)

      for (const input of spec.inputs) {
        const extra: any = {}
        if (input.widget) extra.widget = { name: input.widget.name }
        if (input.shape !== undefined) extra.shape = input.shape
        this.addInput(input.name, input.type, extra)
      }

      if (spec.dynamicStems) {
        for (const stem of DEFAULT_STEMS) {
          for (const name of stemOutputNames(stem)) {
            this.addOutput(name, name.endsWith('(String)') ? PORT.STRING : PORT.AUDIO)
          }
        }
      } else {
        for (const output of spec.outputs) this.addOutput(output.name, output.type)
      }

      if (spec.isOutput) (this as any).is_output_node = true
      if (spec.type === 'pymss_audio_ensemble') syncEnsembleInputs(this, this.widgets?.[0]?.value)
      localizeNodeWithSpec(this, spec)
      if (isWorkflowSeparationNodeType(spec.type)) {
        this.setSize([Math.max(this.size[0], SEPARATE_NODE_MIN_WIDTH), this.size[1]])
      }
    }

    configure(info: any) {
      const normalizedInfo = normalizeBuiltinWidgetLayout(spec, info)
      const incomingTitle = typeof normalizedInfo.title === 'string' ? normalizedInfo.title : ''
      const defaultTitle = !incomingTitle
        || incomingTitle === spec.title
        || incomingTitle === translateNodeTitle(spec)
      let widgets = spec.widgets
      if (spec.type === 'pymss_save_audio') {
        const values = Array.isArray(normalizedInfo.widgets_values) ? normalizedInfo.widgets_values : []
        // The rate sits at index 2 in legacy graphs, including numeric folder names.
        // A trailing ComfyUI button value does not add an output-folder widget.
        const hasFolder = /^\d+$/.test(String(values[2] ?? ''))
          || (values.length >= 6 && !/^\d+$/.test(String(values[1] ?? '')))
        if (hasFolder) {
          widgets = [widgets[0]!, { name: 'output_folder', type: 'text', default: 'Default' }, ...widgets.slice(1)]
        }
      }
      createWidgets(this, widgets)
      super.configure(normalizedInfo)
      ;(this as AnyNode)[DEFAULT_TITLE_FLAG] = defaultTitle
      for (const widget of this.widgets || []) this.properties[widget.name] = widget.value
      if (spec.type === 'pymss_audio_ensemble') {
        syncEnsembleInputs(this, this.widgets?.[0]?.value)
      }
      localizePymssNode(this)
      this.setSize([
        isWorkflowSeparationNodeType(spec.type) ? Math.max(this.size[0], SEPARATE_NODE_MIN_WIDTH) : this.size[0],
        Math.max(this.size[1], this.computeSize()[1]),
      ])
    }

    onSerialize(data: any) {
      for (const widget of this.widgets || []) data.properties[widget.name] = widget.value
      if (usesDefaultTitle(this, spec)) delete data.title
      restoreSerializedLocalizedNames(data.inputs, this.inputs)
      restoreSerializedLocalizedNames(data.outputs, this.outputs)
    }

    onExecute() {
      /* Execution happens in pymss; the canvas is edit-only. */
    }
  }
  ;(klass as any)._pymssSpec = spec
  return klass
}

let registered = false

/** Node type names pymss accepts (bare + pymss_ prefix alias for separates). */
export function allNodeTypes(): string[] {
  const types = new Set<string>()
  for (const spec of Object.values(NODE_SPECS)) {
    types.add(spec.type)
    if (isWorkflowSeparationNodeType(spec.type)) types.add(`pymss_${spec.type}`)
  }
  for (const spec of Object.values(BUILTIN_SPECS)) types.add(spec.type)
  return [...types]
}

export function registerPymssNodes(translator?: NodeTranslator) {
  if (translator) setPymssNodeTranslator(translator)
  applyPymssPortColors()
  if (registered) return
  registered = true
  const register = (spec: NodeSpec, type = spec.type) => {
    const cls = makeNodeClass(spec) as any
    LiteGraph.registerNodeType(type, cls)
    // registerNodeType 用 type 名派生 category(无 '/' 时置空串,覆盖类的
    // static category),导致右键 Add Node 菜单按类别分组为空。补回 spec.category。
    cls.category = translateNodeCategory(spec.category)
    registeredNodeClasses.push({ cls, spec })
  }
  for (const spec of Object.values(NODE_SPECS)) {
    register(spec)
    // pymss registers `pymss_mss_separate` etc. as aliases of the bare names;
    // register the same class under the prefixed name so imported graphs load.
    if (isWorkflowSeparationNodeType(spec.type)) {
      register(spec, `pymss_${spec.type}`)
    }
  }
  for (const spec of Object.values(BUILTIN_SPECS)) {
    register(spec)
  }
}

export { NODE_SPECS, BUILTIN_SPECS }
