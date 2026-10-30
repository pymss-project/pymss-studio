import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test, { after } from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, reactive, ref, shallowRef } from 'vue'
import { parse } from 'vue/compiler-sfc'
import { createServer } from 'vite'
import { setupLitegraphEnvDom } from './_litegraphEnv.mjs'

await setupLitegraphEnvDom()
window.__TAURI_INTERNALS__ = {}
const vite = await createServer({
  configFile: false,
  server: { watch: null, middlewareMode: true, hmr: false, ws: false },
  appType: 'custom', optimizeDeps: { noDiscovery: true },
  ssr: { noExternal: ['@comfyorg/litegraph'] },
})
after(() => vite.close())
const { LGraph } = await vite.ssrLoadModule('@comfyorg/litegraph')
const registry = await vite.ssrLoadModule('/src/litegraph/registerNodes.ts')
const { pluginNodeSpec, selectPluginNodeDeclarations } = await vite.ssrLoadModule('/src/litegraph/nodeSpecs.ts')
const adapter = await vite.ssrLoadModule('/src/litegraph/graphAdapter.ts')
const descriptor = parse(readFileSync(new URL('../src/components/workflow/WorkflowNodeEditorLite.vue', import.meta.url), 'utf8')).descriptor
const source = ts.createSourceFile('editor.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const names = new Set(['loadPluginNodeSpecs', 'flagUnregisteredNodes', 'updatePluginNodeStatus', 'loadDefinition', 'initializeGraph', 'snapshotDefinition', 'reportLoadError'])
const functions = source.statements.filter(node => ts.isFunctionDeclaration(node) && names.has(node.name?.text))
const hook = name => source.statements.find(node => ts.isExpressionStatement(node)
  && ts.isCallExpression(node.expression) && node.expression.expression.getText(source) === name).expression.arguments[0].getText(source)
const palette = source.statements.find(node => ts.isVariableStatement(node)
  && node.declarationList.declarations.some(declaration => declaration.name.getText(source) === 'paletteCategories'))
const code = ts.transpileModule([
  ...functions.map(node => node.getText(source)), palette.getText(source),
  `const mount = ${hook('onMounted')}`, `const unmount = ${hook('onBeforeUnmount')}`,
].join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function setup(overrides = {}) {
  let resolve
  const response = new Promise(done => { resolve = done })
  const type = overrides.type || 'test_async_plugin_node'
  const spec = { type, title: 'Gain', category: 'pymss/plugins', inputs: [], outputs: [], widgets: [{ name: 'gain', type: 'number', default: 2 }], registered: true, ...overrides }
  let canvases = 0
  class Canvas {
    constructor() { canvases++; this.ds = { scale: 1, offset: [0, 0] } }
    setDirty() {}
    stopRendering() {}
    unbindEvents() {}
  }
  const state = {
    computed, window, document, LGraph, LGraphCanvas: Canvas,
    disposed: false, pluginNodeLoadId: 0, invoke: () => response,
    ALL_SPECS: reactive({ ...registry.NODE_SPECS, ...registry.BUILTIN_SPECS }),
    NODE_SPECS: registry.NODE_SPECS, BUILTIN_SPECS: registry.BUILTIN_SPECS,
    pluginNodeSpec, selectPluginNodeDeclarations,
    registerPymssNodes: registry.registerPymssNodes, registerPluginNodeSpecs: registry.registerPluginNodeSpecs,
    graphRef: shallowRef(null), canvasRef: shallowRef(null), canvasEl: ref(document.createElement('canvas')),
    definition: ref({ nodes: [{ id: 1, type, pos: [0, 0], size: [180, 80], flags: {}, order: 0, mode: 0, inputs: [], outputs: [], properties: {}, widgets_values: [7] }], links: [], version: 0.4 }),
    props: { readonly: false, createStarter: false, defaultDevice: 'auto', defaultFormat: 'wav' },
    unregisteredPluginNodes: ref(new Map()), ready: ref(false), loadError: ref(''),
    knownPluginNodeTypes: new Set(), originalPluginNodeStyles: new WeakMap(),
    graphInitialized: false, awaitingInitialDefinition: false, configuringGraph: false,
    locale: ref('en'), nodeTranslator: key => key, t: key => key,
    translateNodeCategory: value => value, translateNodeTitle: value => value.title,
    comfyToLitegraph: adapter.comfyToLitegraph, litegraphToComfy: adapter.litegraphToComfy,
    isGraphWorkflowDefinition: value => Array.isArray(value.nodes),
    applyTheme() {}, applyPymssPortColors() {}, configureGraphNode() {}, restoreViewport() {},
    writeGraphDefaults() {}, resetHistory() {}, emit() {}, scheduleSnap() {},
    installCanvasDpi: () => () => {}, disposeCanvasDpi: null, executionResizeObserver: null,
    drawRunningNodes() {}, disableEmptyCanvasDoubleClick() {}, restoreReadonlyAfterKeyup() {},
    onCanvasKey() {}, onEditorKey() {}, pendingSnap: 0, cancelAnimationFrame,
    studioLog: { error() {} }, console,
  }
  const context = vm.createContext(state)
  const editor = vm.runInContext(`${code}\n({ mount, unmount, paletteCategories })`, context)
  return { state: context, editor, resolve: () => resolve({ nodes: [spec] }), type, canvases: () => canvases }
}

test('persisted plugin nodes restore only after declarations arrive and update the palette', async () => {
  const { state, editor, resolve, type, canvases } = setup()
  try {
    assert.ok(!editor.paletteCategories.value.some(group => group.items.some(item => item.type === type)))
    const pending = editor.mount()
    assert.equal(canvases(), 0)
    assert.equal(state.graphRef.value, null)
    resolve()
    await pending
    assert.equal(canvases(), 1)
    assert.equal(state.ready.value, true)
    const node = state.graphRef.value.nodes[0]
    assert.equal(node.type, type)
    assert.ok(!node.has_errors)
    assert.equal(node.widgets[0].name, 'gain')
    assert.equal(node.widgets[0].value, 7)
    assert.ok(editor.paletteCategories.value.some(group => group.items.some(item => item.type === type)))
  } finally {
    editor.unmount()
    registry.registerPluginNodeSpecs([])
  }
})

test('closing while declarations load does not create a canvas or register stale nodes', async () => {
  const { state, editor, resolve, type, canvases } = setup()
  const pending = editor.mount()
  editor.unmount()
  resolve()
  await pending
  assert.equal(canvases(), 0)
  assert.equal(state.graphRef.value, null)
  assert.ok(!registry.allNodeTypes().includes(type))
})

test('unavailable nodes retain the concrete diagnostic rather than replacing it with the plugin id', async () => {
  const reason = 'Missing plugin dependency: funasr'
  const { state, editor, resolve } = setup({ registered: false, pluginId: 'pymss.asr', unavailableReason: reason })
  try {
    const pending = editor.mount()
    resolve()
    await pending
    const node = state.graphRef.value.nodes[0]
    assert.equal(node.pymssMissingPlugin, reason)
    assert.equal(node.color, '#5a2d2d')
  } finally {
    editor.unmount()
    registry.registerPluginNodeSpecs([])
  }
})

test('a failed plugin declaration cannot flag a builtin node as unavailable', async () => {
  const { state, editor, resolve, type } = setup({ type: 'pymss_load_audio', registered: false, pluginId: 'example.invalid', unavailableReason: 'reserved node type' })
  try {
    const pending = editor.mount()
    resolve()
    await pending
    assert.equal(state.unregisteredPluginNodes.value.has(type), false)
    assert.equal(state.graphRef.value.nodes[0].pymssMissingPlugin, undefined)
  } finally {
    editor.unmount()
    registry.registerPluginNodeSpecs([])
  }
})
