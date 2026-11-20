import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test, { after } from 'node:test'
import { createServer } from 'vite'
import { reactive } from 'vue'
import { setupLitegraphEnvDom } from './_litegraphEnv.mjs'

await setupLitegraphEnvDom()
const vite = await createServer({
  configFile: false,
  server: { watch: null, middlewareMode: true, hmr: false, ws: false },
  appType: 'custom',
  optimizeDeps: { noDiscovery: true },
  ssr: { noExternal: ['@comfyorg/litegraph'] },
})
after(() => vite.close())

const { LGraph, LGraphCanvas, LiteGraph } = await vite.ssrLoadModule('@comfyorg/litegraph')
const {
  registerPymssNodes,
  registerPluginNodeSpecs,
  allNodeTypes,
  NODE_SPECS,
  BUILTIN_SPECS,
  localizePymssNode,
  setSeparateStems,
  setPymssNodeTranslator,
  applyPymssPortColors,
  PORT_COLORS,
  SEPARATE_NODE_MIN_WIDTH,
} = await vite.ssrLoadModule('/src/litegraph/registerNodes.ts')
const adapter = await vite.ssrLoadModule('/src/litegraph/graphAdapter.ts')
const { pluginNodeSpec, selectPluginNodeDeclarations } = await vite.ssrLoadModule('/src/litegraph/nodeSpecs.ts')
registerPymssNodes()

test('plugin numeric widgets preserve manifest bounds and actual step units in LiteGraph', () => {
  const type = 'test_plugin_bounded_number'
  try {
    registerPluginNodeSpecs([pluginNodeSpec({
      type,
      widgets: [
        { name: 'ratio', type: 'number', default: 0.5, min: 0, max: 1, step: 0.25 },
        { name: 'frequency', type: 'number', default: 100, min: 0, max: 200, step: 10 },
      ],
    })])
    const graph = new LGraph()
    const node = LiteGraph.createNode(type)
    graph.add(node)
    const options = { e: undefined, node, canvas: { graph_mouse: [0, 0] } }
    const [ratio, frequency] = node.widgets
    ratio.incrementValue(options)
    assert.equal(ratio.value, 0.75)
    ratio.decrementValue(options)
    assert.equal(ratio.value, 0.5)
    ratio.setValue(-1, options)
    assert.equal(ratio.value, 0)
    ratio.setValue(2, options)
    assert.equal(ratio.value, 1)
    frequency.incrementValue(options)
    assert.equal(frequency.value, 110)
    assert.deepEqual(adapter.litegraphToComfy(graph.serialize()).nodes[0].widgets_values, [1, 110])
  } finally {
    registerPluginNodeSpecs([])
  }
})

test('a conflicting inactive declaration cannot replace the live node owner', () => {
  const active = { type: 'SharedNode', pluginId: 'example.owner', registered: true, title: 'Active' }
  const blocked = { type: 'SharedNode', pluginId: 'example.other', registered: false, title: 'Blocked' }
  for (const declarations of [[active, blocked], [blocked, active]]) {
    assert.deepEqual(selectPluginNodeDeclarations(declarations), [active])
  }
})

test('plugin slider values remain numeric after editing and graph round-trips', () => {
  const type = 'test_plugin_slider'
  try {
    registerPluginNodeSpecs([pluginNodeSpec({
      type, widgets: [{ name: 'gain', type: 'slider', default: 0.5, min: 0, max: 1, step: 0.25 }],
    })])
    const graph = new LGraph()
    const node = LiteGraph.createNode(type)
    graph.add(node)
    const gain = node.widgets[0]
    const options = { e: undefined, node, canvas: { graph_mouse: [0, 0] } }
    gain.incrementValue(options)
    assert.equal(gain.value, 0.75)
    const saved = graph.serialize()
    assert.deepEqual(adapter.litegraphToComfy(saved).nodes[0].widgets_values, [0.75])
    const restored = new LGraph()
    restored.configure(saved)
    const restoredNode = restored.getNodeById(node.id)
    assert.equal(typeof restoredNode.widgets[0].value, 'number')
    const restoredOptions = { ...options, node: restoredNode }
    restoredNode.widgets[0].incrementValue(restoredOptions)
    restoredNode.widgets[0].incrementValue(restoredOptions)
    assert.equal(restoredNode.widgets[0].value, 1)
    restoredNode.widgets[0].setValue(-1, restoredOptions)
    assert.equal(restoredNode.widgets[0].value, 0)
  } finally {
    registerPluginNodeSpecs([])
  }
})

test('plugin node declarations update new instances and remove stale creation entries', () => {
  const type = 'test_plugin_gain'
  const spec = { type, title: 'Gain', category: 'pymss/plugins', inputs: [], outputs: [], widgets: [{ name: 'gain', type: 'number', default: 2 }] }
  const builtin = LiteGraph.registered_node_types.AudioAdjustVolume
  try {
    registerPluginNodeSpecs([spec])
    const original = LiteGraph.createNode(type)
    assert.equal(original.widgets[0].value, 2)
    registerPluginNodeSpecs([{ ...spec, title: 'Updated Gain', widgets: [{ name: 'amount', type: 'number', default: 6 }] }])
    const updated = LiteGraph.createNode(type)
    assert.equal(updated.widgets[0].name, 'amount')
    assert.equal(updated.widgets[0].value, 6)
    assert.equal(original.widgets[0].name, 'gain')
    registerPluginNodeSpecs([{ ...spec, type: 'AudioAdjustVolume' }])
    assert.equal(LiteGraph.registered_node_types.AudioAdjustVolume, builtin)
    assert.ok(!allNodeTypes().includes(type))
    assert.equal(LiteGraph.registered_node_types[type], undefined)
    assert.equal(original.serialize().type, type)
  } finally {
    registerPluginNodeSpecs([])
  }
})

const fixture = name => JSON.parse(readFileSync(new URL(`./fixtures/comfy-mss/${name}.json`, import.meta.url), 'utf8'))
const jsonClone = value => JSON.parse(JSON.stringify(value))
const messages = {
  en: JSON.parse(readFileSync(new URL('../src/i18n/en.json', import.meta.url), 'utf8')),
  'zh-CN': JSON.parse(readFileSync(new URL('../src/i18n/zh-CN.json', import.meta.url), 'utf8')),
}
const translator = locale => (key, named = {}) => {
  const value = key.split('.').reduce((current, part) => current?.[part], messages[locale])
  if (typeof value !== 'string') return key
  return value.replace(/\{(\w+)\}/g, (_, name) => String(named[name] ?? `{${name}}`))
}
function load(source) {
  const graph = new LGraph()
  graph.configure(adapter.comfyToLitegraph(source))
  return graph
}
const exportGraph = graph => jsonClone(adapter.litegraphToComfy(graph.serialize()))

test('advanced editor locale catalogs cover every registered node type', () => {
  const expected = Object.keys({ ...NODE_SPECS, ...BUILTIN_SPECS }).sort()
  for (const locale of ['en', 'zh-CN']) {
    const actual = Object.keys(messages[locale].workflows.advancedEditor.nodes).sort()
    // Locale catalogs may additionally cover dynamic plugin node types
    // (manifest [[contributions.nodes]]); every static type must be present.
    const missing = expected.filter(type => !actual.includes(type))
    assert.deepEqual(missing, [], `${locale} missing: ${missing.join(', ')}`)
  }
})

test('every advanced editor node type can be created, serialized and restored', () => {
  for (const type of allNodeTypes()) {
    const graph = new LGraph()
    const node = LiteGraph.createNode(type)
    assert.ok(node, `create ${type}`)
    graph.add(node)

    const exported = exportGraph(graph)
    assert.equal(exported.nodes.length, 1, `serialize ${type}`)
    const exportedType = /^(mss|vr|custom_mss)_separate(_list)?$/.test(type) ? `pymss_${type}` : type
    assert.equal(exported.nodes[0].type, exportedType, `export type ${type}`)

    const restored = load(exported)
    assert.equal(restored.nodes.length, 1, `restore ${type}`)
    assert.equal(restored.nodes[0].type, exportedType, `restore type ${type}`)
  }
})

test('configurable builtin nodes expose runtime-compatible widget layouts', () => {
  const expected = {
    SaveAudio: ['filename_prefix'],
    SaveAudioMP3: ['filename_prefix', 'quality'],
    SaveAudioOpus: ['filename_prefix', 'bitrate'],
    SaveAudioAdvanced: ['filename_prefix', 'format', 'quality'],
    TrimAudioDuration: ['start_index', 'duration'],
    AudioAdjustVolume: ['volume'],
    EmptyAudio: ['duration', 'sample_rate', 'channels'],
    AudioEqualizer3Band: ['low_gain_dB', 'low_freq', 'mid_gain_dB', 'mid_freq', 'mid_q', 'high_gain_dB', 'high_freq'],
    StringSubstring: ['string', 'start', 'end'],
    StringTrim: ['string', 'mode'],
    CaseConverter: ['string', 'mode'],
    RegexExtract: ['string', 'regex_pattern', 'mode', 'group_index'],
  }
  for (const [type, widgets] of Object.entries(expected)) {
    const node = LiteGraph.createNode(type)
    assert.deepEqual(node.widgets.map(widget => widget.name), widgets, type)
  }
  assert.deepEqual(LiteGraph.createNode('SaveAudioAdvanced').outputs.map(output => output.name), ['audio'])
})

test('MSS params accept model defaults and unrestricted positive integer overrides', () => {
  const graph = new LGraph()
  const node = LiteGraph.createNode('pymss_mss_params')
  graph.add(node)
  const overlap = node.widgets.find(widget => widget.name === 'overlap_size')
  const chunk = node.widgets.find(widget => widget.name === 'chunk_size')

  assert.equal(overlap.type, 'text')
  assert.equal(chunk.type, 'text')
  assert.equal(overlap.value, 'Default')
  assert.equal(chunk.value, 'Default')

  overlap.value = '48000'
  chunk.value = '485100'
  const restored = load(exportGraph(graph)).nodes[0]
  assert.equal(restored.widgets.find(widget => widget.name === 'overlap_size').value, '48000')
  assert.equal(restored.widgets.find(widget => widget.name === 'chunk_size').value, '485100')
})

test('save audio sample-rate choices stop at 48 kHz', () => {
  const node = LiteGraph.createNode('pymss_save_audio')
  const sampleRate = node.widgets.find(widget => widget.name === 'sample_rate')
  assert.deepEqual(sampleRate.options.values, ['32000', '44100', '48000'])
})

test('separate nodes shrink after switching from six stems to two', () => {
  const node = LiteGraph.createNode('mss_separate')
  assert.ok(node.size[0] >= SEPARATE_NODE_MIN_WIDTH)
  setSeparateStems(node, ['vocals', 'drums', 'bass', 'guitar', 'piano', 'other'])
  const expandedHeight = node.size[1]

  setSeparateStems(node, ['vocals', 'instrumental'])

  assert.ok(node.size[1] < expandedHeight)
  assert.equal(node.size[1], node.computeSize()[1])
  assert.equal(node.outputs.length, 4)
})

test('legacy audio preview nodes are hidden from creation menus but remain importable', () => {
  assert.equal(NODE_SPECS.PreviewAudio.hidden, true)
  const preview = LiteGraph.registered_node_types.PreviewAudio
  assert.equal(preview.skip_list, true)
  assert.equal(Object.keys(LiteGraph.registered_node_types).includes('PreviewAudio'), false)
  const graph = load({ nodes: [{ id: 1, type: 'PreviewAudio', pos: [10, 20], size: [200, 80],
    inputs: [{ name: 'audio', type: 'AUDIO', link: null }], outputs: [], widgets_values: [] }], links: [] })
  assert.equal(graph.nodes[0].type, 'PreviewAudio')
  const exported = exportGraph(graph)
  assert.equal(exported.nodes[0].type, 'PreviewAudio')
  assert.deepEqual(exported.nodes[0].inputs.map(input => input.name), ['audio'])
})

test('separate stem changes preserve matching links and drop only removed stems', () => {
  const graph = new LGraph()
  const separate = LiteGraph.createNode('mss_separate')
  const vocalsSave = LiteGraph.createNode('pymss_save_audio')
  const instrumentalSave = LiteGraph.createNode('pymss_save_audio')
  graph.add(separate)
  graph.add(vocalsSave)
  graph.add(instrumentalSave)
  setSeparateStems(separate, ['vocals', 'instrumental'])
  separate.connect(0, vocalsSave, 0)
  separate.connect(2, instrumentalSave, 0)

  setSeparateStems(separate, ['Vocals', 'drums', 'bass', 'other'])

  assert.deepEqual(separate.outputs.map(output => output.name), [
    'Vocals (Audio)', 'Vocals (String)',
    'drums (Audio)', 'drums (String)',
    'bass (Audio)', 'bass (String)',
    'other (Audio)', 'other (String)',
  ])
  assert.notEqual(vocalsSave.inputs[0].link, null)
  assert.equal(instrumentalSave.inputs[0].link, null)
})

test('placeholder separate outputs retain positional links when a model is selected', () => {
  const graph = new LGraph()
  const separate = LiteGraph.createNode('mss_separate')
  const save = LiteGraph.createNode('pymss_save_audio')
  graph.add(separate)
  graph.add(save)
  separate.connect(0, save, 0)

  setSeparateStems(separate, ['vocals', 'instrumental'])

  assert.notEqual(save.inputs[0].link, null)
  assert.equal(separate.outputs[0].name, 'vocals (Audio)')
})

test('separate stem changes preserve reroute chains on retained links', () => {
  const graph = new LGraph()
  const separate = LiteGraph.createNode('mss_separate')
  const save = LiteGraph.createNode('pymss_save_audio')
  graph.add(separate)
  graph.add(save)
  setSeparateStems(separate, ['vocals', 'instrumental'])
  const originalLink = separate.connect(0, save, 0)
  assert.ok(originalLink)
  const firstReroute = graph.createReroute([180, 120], originalLink)
  const lastReroute = graph.createReroute([240, 160], originalLink)
  assert.equal(lastReroute.parentId, firstReroute.id)
  assert.equal(originalLink.parentId, lastReroute.id)

  setSeparateStems(separate, ['Vocals', 'drums', 'bass', 'other'])

  const restoredLink = graph.getLink(save.inputs[0].link)
  const restoredFirst = graph.getReroute(firstReroute.id)
  const restoredLast = graph.getReroute(lastReroute.id)
  assert.equal(restoredLink.parentId, lastReroute.id)
  assert.equal(restoredLast.parentId, firstReroute.id)
  assert.deepEqual(Array.from(restoredFirst.pos), [180, 120])
  assert.deepEqual(Array.from(restoredLast.pos), [240, 160])
})

test('separate nodes restore at a readable minimum width without shrinking wider layouts', () => {
  const graph = new LGraph()
  const node = LiteGraph.createNode('mss_separate')
  graph.add(node)
  const source = exportGraph(graph)
  source.nodes[0].size = [180, source.nodes[0].size[1]]

  const restored = load(source).nodes[0]
  assert.equal(restored.size[0], SEPARATE_NODE_MIN_WIDTH)

  restored.setSize([360, restored.size[1]])
  setSeparateStems(restored, ['vocals', 'instrumental'])
  assert.equal(restored.size[0], 360)
})

test('workflow port types use distinct colors for links and connection states', () => {
  const canvas = {
    default_connection_color_byType: {},
    default_connection_color_byTypeOff: {},
  }
  applyPymssPortColors(canvas)

  const types = ['AUDIO', 'STRING', 'PYMSS_MSS_PARAMS', 'PYMSS_VR_PARAMS', 'BOOLEAN', 'COMBO']
  assert.equal(new Set(types.map(type => PORT_COLORS[type])).size, types.length)
  for (const type of types) {
    assert.equal(LGraphCanvas.link_type_colors[type], PORT_COLORS[type])
    assert.equal(canvas.default_connection_color_byType[type], PORT_COLORS[type])
    assert.ok(canvas.default_connection_color_byTypeOff[type])
  }
})

test('legacy Studio builtin widget layouts migrate without shifting values', () => {
  const cases = [
    ['SaveAudio', [], ['audio']],
    ['SaveAudioMP3', ['128k'], ['audio', '128k']],
    ['SaveAudioOpus', ['96k'], ['audio', '96k']],
    ['SaveAudioAdvanced', ['mp3', '44100', 'FLOAT', 'PCM_24', '192k'], ['audio', 'mp3', '192k']],
    ['AudioConcat', ['front'], ['before']],
    ['StringTrim', ['left'], ['', 'Left']],
    ['CaseConverter', ['title'], ['', 'Title Case']],
    ['RegexExtract', ['all'], ['', '', 'All Matches', 1]],
  ]
  for (const [type, legacy, expected] of cases) {
    const graph = new LGraph()
    const node = LiteGraph.createNode(type)
    graph.add(node)
    const source = exportGraph(graph)
    source.nodes[0].widgets_values = legacy
    if (type === 'SaveAudioAdvanced') source.nodes[0].outputs = []
    const restored = load(source).nodes[0]
    assert.deepEqual(restored.widgets.map(widget => widget.value), expected, type)
    if (type === 'SaveAudioAdvanced') {
      assert.deepEqual(restored.outputs.map(output => output.name), ['audio'])
    }
  }
})

test('legacy AudioMerge graphs retain peak protection and their merge method', () => {
  for (const method of ['add', 'subtract', 'mean', 'average']) {
    const graph = new LGraph()
    graph.add(LiteGraph.createNode('AudioMerge'))
    const source = exportGraph(graph)
    source.nodes[0].widgets_values = [method]
    source.nodes[0].inputs = source.nodes[0].inputs.filter(input => input.name !== 'normalize')
    delete source.nodes[0].properties.normalize

    const restored = load(source)
    assert.deepEqual(restored.nodes[0].widgets.map(widget => widget.value), [method, true])
    assert.deepEqual(exportGraph(restored).nodes[0].widgets_values, [method, true])
  }
})

test('AudioMerge normalization toggle survives editing, export and reload', () => {
  const graph = new LGraph()
  const merge = LiteGraph.createNode('AudioMerge')
  graph.add(merge)
  const normalize = merge.widgets.find(widget => widget.name === 'normalize')
  assert.equal(normalize.value, true)
  assert.deepEqual(merge.inputs.map(input => input.name), ['audio1', 'audio2', 'merge_method', 'normalize'])
  assert.equal(merge.inputs[3].type, 'BOOLEAN')
  merge.widgets[0].setValue('subtract', { e: undefined, node: merge, canvas: { graph_mouse: [0, 0] } })
  normalize.setValue(false, { e: undefined, node: merge, canvas: { graph_mouse: [0, 0] } })

  const exported = exportGraph(graph)
  assert.deepEqual(exported.nodes[0].widgets_values, ['subtract', false])
  assert.equal(exported.nodes[0].properties.normalize, false)
  const restored = load(exported)
  assert.deepEqual(restored.nodes[0].widgets.map(widget => widget.value), ['subtract', false])
  assert.deepEqual(exportGraph(restored).nodes[0].widgets_values, ['subtract', false])
})

test('advanced editor labels follow locale without changing workflow data', () => {
  const source = fixture('example_mss_separate')
  const en = translator('en')
  const zh = translator('zh-CN')
  setPymssNodeTranslator(en)
  const graph = load(source)
  for (const node of graph.nodes) localizePymssNode(node, en)
  const before = exportGraph(graph)

  setPymssNodeTranslator(zh)
  for (const node of graph.nodes) localizePymssNode(node, zh)
  const separate = graph.nodes.find(node => String(node.type).includes('mss_separate'))
  assert.equal(separate.title, 'MSS 分离')
  assert.equal(separate.widgets.find(widget => widget.name === 'model_name').label, '模型名称')
  assert.equal(separate.widgets.find(widget => widget.name === 'source').label, '模型源')
  assert.equal(separate.inputs.find(input => input.name === 'audio').label, '音频')
  assert.match(separate.outputs[0].label, /\(音频\)$/)
  assert.deepEqual(exportGraph(graph), before, 'display translations must not leak into serialized workflow data')

  const created = LiteGraph.createNode('pymss_audio_ensemble')
  assert.equal(created.title, '音频集成')
  assert.equal(created.widgets.find(widget => widget.name === 'weight_1').label, '权重 1')
  assert.equal(created.inputs[0].localized_name, '音频 1')

  created.title = 'Custom ensemble'
  created.inputs[0].label = 'Primary mix'
  localizePymssNode(created, en)
  assert.equal(created.title, 'Custom ensemble', 'custom node titles must not be overwritten by locale changes')
  assert.equal(created.inputs[0].label, 'Primary mix', 'custom port labels must not be overwritten by locale changes')
  const customized = created.serialize()
  assert.equal(customized.title, 'Custom ensemble')
  assert.equal(customized.inputs[0].label, 'Primary mix')
  setPymssNodeTranslator(en)
})

test('plugin source ports use generic labels without changing their names or saved values', () => {
  const type = 'test_file_source_label'
  try {
    registerPluginNodeSpecs([pluginNodeSpec({ type, inputs: [{ name: 'source', type: 'STRING' }], outputs: [], widgets: [] })])
    const graph = new LGraph()
    const node = LiteGraph.createNode(type)
    graph.add(node)
    const before = exportGraph(graph)
    localizePymssNode(node, translator('zh-CN'))
    assert.equal(node.inputs[0].label, '来源')
    assert.equal(node.inputs[0].name, 'source')
    assert.deepEqual(exportGraph(graph), before)
    localizePymssNode(node, translator('en'))
    assert.equal(node.inputs[0].label, 'Source')
  } finally {
    registerPluginNodeSpecs([])
    setPymssNodeTranslator(translator('en'))
  }
})

test('advanced editor localizes nested LiteGraph context menus', () => {
  const en = translator('en')
  setPymssNodeTranslator(translator('zh-CN'))
  const originalBodyRect = document.body.getBoundingClientRect
  document.body.getBoundingClientRect = () => new window.DOMRect(0, 0, 1280, 720)
  let menu

  try {
    menu = new LiteGraph.ContextMenu([
      { content: 'Mode', submenu: { options: ['Always', 'Never'] } },
      'Convert to Subgraph 🆕',
    ], {
      event: new window.MouseEvent('contextmenu', { clientX: 20, clientY: 20 }),
      title: 'vr_separate',
    })
    assert.equal(menu.root.querySelector('.litemenu-title')?.textContent?.trim(), 'VR 分离')
    const entries = [...menu.root.querySelectorAll('.litemenu-entry')]
    const topEntry = entries[0]
    assert.equal(topEntry?.textContent?.trim(), '运行模式')
    assert.equal(entries[1]?.textContent?.trim(), '转换为子图 🆕')
    topEntry?.click()

    const submenu = menu.current_submenu
    assert.ok(submenu, 'clicking the mode entry should open its submenu')
    assert.deepEqual(
      [...submenu.root.querySelectorAll('.litemenu-entry')].map(entry => entry.textContent?.trim()),
      ['始终运行', '从不运行'],
    )
  } finally {
    menu?.close()
    document.body.getBoundingClientRect = originalBodyRect
    setPymssNodeTranslator(en)
  }
})

test('reactive workflow definitions load without cloning proxies or mutating their source', () => {
  const source = reactive(fixture('example_ensemble'))
  const before = jsonClone(source)
  const converted = adapter.comfyToLitegraph(source)
  assert.doesNotThrow(() => structuredClone(converted.extra))
  const exported = exportGraph(load(source))
  assert.equal(exported.nodes.length, source.nodes.length)
  assert.deepEqual(exported.links, source.links)
  assert.deepEqual(jsonClone(source), before)
})

test('exported tuples declare schema 0.4 and load without a Studio adapter', () => {
  const source = fixture('example_mss_separate')
  // Earlier Studio versions stored tuples under version 1, including numeric-key objects.
  source.version = 1
  source.links = source.links.map(link => ({ ...link }))
  const before = jsonClone(source)
  const exported = jsonClone(adapter.litegraphToComfy(source))
  assert.equal(exported.version, 0.4)
  assert.ok(exported.links.every(link => Array.isArray(link) && link.length === 6))
  assert.deepEqual(source, before)
  const graph = new LGraph()
  graph.configure(exported)
  assert.deepEqual(exportGraph(graph).links, exported.links)
  assert.equal(graph.nodes.length, source.nodes.length)
})

test('new save nodes and upstream five-value imports use the same widget order', () => {
  const values = ['flac', '32000', 'PCM_16', 'PCM_24', '192k']
  const source = fixture('example_mss_separate')
  const save = source.nodes.find(node => node.type === 'pymss_save_audio')
  save.widgets_values = values
  save.inputs = save.inputs.filter(input => input.name !== 'output_folder')
  const graph = load(source)
  const node = graph.getNodeById(save.id)
  assert.deepEqual(node.widgets.map(w => w.name), ['output_format', 'sample_rate', 'wav_bit_depth', 'flac_bit_depth', 'mp3_bit_rate'])
  assert.deepEqual(node.widgets.map(w => w.value), values)
  assert.deepEqual(LiteGraph.createNode('pymss_save_audio').widgets.map(w => w.name), node.widgets.map(w => w.name))
  node.widgets.find(w => w.name === 'sample_rate').value = '48000'
  const exported = exportGraph(graph)
  assert.deepEqual(exported.nodes.find(n => n.id === save.id).widgets_values, ['flac', '48000', 'PCM_16', 'PCM_24', '192k'])
  assert.deepEqual(exportGraph(load(exported)).links, exported.links)
  assert.deepEqual(load(exported).getNodeById(save.id).widgets.map(w => w.value), ['flac', '48000', 'PCM_16', 'PCM_24', '192k'])
})

test('legacy six-value save nodes retain their output folder across editing and reload', () => {
  const source = fixture('example_mss_separate')
  const save = source.nodes.find(node => node.type === 'pymss_save_audio')
  const values = ['mp3', 'stems', '48000', 'FLOAT', 'PCM_16', '192k']
  save.widgets_values = values
  const graph = load(source)
  const node = graph.getNodeById(save.id)
  assert.equal(node.widgets.find(w => w.name === 'output_folder').value, 'stems')
  assert.equal(node.widgets.find(w => w.name === 'sample_rate').value, '48000')
  const exported = exportGraph(graph)
  assert.deepEqual(exported.nodes.find(n => n.id === save.id).widgets_values, values)
  assert.deepEqual(load(exported).getNodeById(save.id).widgets.map(w => w.value), values)
})

test('save layouts distinguish numeric folders from trailing ComfyUI button values', () => {
  for (const values of [
    ['wav', '2026', '48000', 'FLOAT', 'PCM_24', '320k'],
    ['wav', '48000', 'FLOAT', 'PCM_24', '320k', null],
  ]) {
    const graph = new LGraph()
    const node = LiteGraph.createNode('pymss_save_audio')
    graph.add(node)
    node.configure({ ...node.serialize(), widgets_values: values })
    const folder = node.widgets.find(w => w.name === 'output_folder')
    assert.equal(folder?.value, values[1] === '2026' ? '2026' : undefined)
    assert.equal(node.widgets.find(w => w.name === 'sample_rate').value, '48000')
    const expected = folder ? values : values.slice(0, 5)
    assert.deepEqual(exportGraph(load(exportGraph(graph))).nodes[0].widgets_values, expected)
  }
})

test('prefixed legacy custom separator layouts migrate for both output variants', () => {
  for (const suffix of ['', '_list']) {
    const graph = new LGraph()
    const node = LiteGraph.createNode(`pymss_custom_mss_separate${suffix}`)
    graph.add(node)
    const source = exportGraph(graph)
    source.nodes[0].widgets_values = ['custom.ckpt', 'bs_roformer', 'cpu', true, 'modelscope', '0,1', true]
    const restored = load(source).nodes[0]
    assert.deepEqual(restored.widgets.map(w => w.value), ['custom.ckpt', 'bs_roformer', 'cpu', '0,1', true])
  }
})

test('custom separator architecture choices survive editing and reload', () => {
  for (const suffix of ['', '_list']) {
    for (const architecture of ['auto', 'bs_conformer', 'mel_band_conformer']) {
      const graph = new LGraph()
      const node = LiteGraph.createNode(`pymss_custom_mss_separate${suffix}`)
      graph.add(node)
      const widget = node.widgets.find(w => w.name === 'model_type')
      assert.ok(widget.options.values.includes(architecture))
      widget.value = architecture
      const restored = load(exportGraph(graph)).getNodeById(node.id)
      assert.equal(restored.widgets.find(w => w.name === 'model_type').value, architecture)
    }
  }
})

test('ensemble algorithm, all weights and audio connections survive editing and reload', () => {
  const source = fixture('example_ensemble')
  const ensemble = source.nodes.find(node => node.type === 'pymss_audio_ensemble')
  const values = ['2', 'median_fft', '0.25', '1.75', '2', '3', '4', '5', '6', '7', '8', '9']
  ensemble.widgets_values = values
  const graph = load(source)
  const node = graph.getNodeById(ensemble.id)
  assert.deepEqual(node.widgets.map(w => w.value), values)
  node.widgets.find(w => w.name === 'weight_2').value = '0.5'
  const exported = exportGraph(graph)
  assert.deepEqual(exported.nodes.find(n => n.id === ensemble.id).widgets_values, values.map((v, i) => i === 3 ? '0.5' : v))
  assert.deepEqual(exported.links, source.links)
  assert.deepEqual(exportGraph(load(exported)), exported)
})

test('ensemble input-count edits keep retained links and expose connectable audio inputs', () => {
  const graph = new LGraph()
  const ensemble = LiteGraph.createNode('pymss_audio_ensemble')
  const input = LiteGraph.createNode('pymss_load_audio')
  graph.add(ensemble)
  graph.add(input)
  assert.deepEqual(ensemble.inputs.map(slot => slot.name), ['audio_1', 'audio_2'])
  input.connect(0, ensemble, 0)
  const setCount = value => ensemble.widgets[0].setValue(value, { node: ensemble, canvas: { graph_mouse: [0, 0] } })
  setCount('4')
  assert.deepEqual(ensemble.inputs.map(slot => slot.name), ['audio_1', 'audio_2', 'audio_3', 'audio_4'])
  input.connect(0, ensemble, 3)
  setCount('2')
  assert.equal(ensemble.inputs.length, 2)
  assert.equal(graph.links.size, 1)
  assert.notEqual(ensemble.inputs[0].link, null)
  assert.equal(ensemble.widgets.length, 12, 'inactive weights still serialize')
  assert.equal(ensemble.widgets.filter(w => !w.hidden).length, 4)
  assert.equal(load(exportGraph(graph)).links.size, 1)
})

test('all separate aliases and list variants have editable widgets and stable outputs', () => {
  for (const base of ['mss_separate', 'vr_separate', 'custom_mss_separate']) {
    for (const suffix of ['', '_list']) {
      for (const prefix of ['', 'pymss_']) {
        const type = `${prefix}${base}${suffix}`
        assert.ok(allNodeTypes().includes(type), type)
        const graph = new LGraph()
        const node = LiteGraph.createNode(type)
        graph.add(node)
        assert.ok(node.widgets.some(w => w.name === 'model_name'), type)
        assert.ok(node.widgets.some(w => w.name === 'device'), type)
        node.widgets.find(w => w.name === 'model_name').value = 'model.ckpt'
        node.widgets.find(w => w.name === 'device').value = 'cpu'
        if (suffix) assert.deepEqual(node.outputs.map(slot => slot.name), ['audios', 'stem_names'])
        const restored = load(exportGraph(graph)).nodes[0]
        assert.equal(restored.widgets.find(w => w.name === 'model_name').value, 'model.ckpt')
        assert.equal(restored.widgets.find(w => w.name === 'device').value, 'cpu')
        assert.equal(restored.outputs.length, node.outputs.length)
      }
    }
  }
})

test('the add-node menus list the six Comfy-MSS separators once and keep legacy types importable', () => {
  const expected = ['pymss_mss_separate', 'pymss_vr_separate', 'pymss_custom_mss_separate']
    .flatMap(type => [type, `${type}_list`]).sort()
  const specs = { ...NODE_SPECS, ...BUILTIN_SPECS }
  const toolbar = Object.values(specs).filter(spec => !spec.hidden)
  const visible = category => LiteGraph.getNodeTypesInCategory(category).filter(node => !node.skip_list)
  const nativeMenu = visible(LiteGraph.registered_node_types.pymss_mss_separate.category)
  assert.deepEqual(toolbar.filter(spec => spec.category === 'pymss/separate').map(spec => spec.type).sort(), expected)
  assert.deepEqual(nativeMenu.map(node => node.type).sort(), expected)
  for (const type of ['LoadAudio', 'PreviewAudio', ...expected.map(type => type.slice('pymss_'.length))]) {
    const cls = LiteGraph.registered_node_types[type]
    assert.ok(LiteGraph.createNode(type), type)
    assert.ok(cls.skip_list, type)
    assert.ok(!visible(cls.category).includes(cls), type)
    assert.ok(!toolbar.some(spec => spec.type === type), type)
  }
  const custom = LiteGraph.createNode('pymss_custom_mss_separate')
  assert.equal(custom.widgets.find(widget => widget.name === 'model_type').value, 'auto')
  assert.ok(custom.widgets.find(widget => widget.name === 'device').options.values.includes('mlx'))
})

test('real fixtures retain every node and link through two editor round-trips without Python', () => {
  for (const name of ['example_mss_separate', 'example_ensemble', 'example_vr_separate', 'example_custom_mss_separate', 'example_batch_separate']) {
    const source = fixture(name)
    const exported = exportGraph(load(source))
    const restored = exportGraph(load(exported))
    assert.equal(exported.nodes.length, source.nodes.length, name)
    assert.deepEqual(exported.links, source.links, name)
    assert.deepEqual(restored, exported, name)
  }
})

const __dirname = dirname(fileURLToPath(import.meta.url))

// Python interpreter used for pymss round-trip checks. Resolution order:
// PYMSS_STUDIO_TEST_PYTHON env var > project venv (.venv/bin/python) > python3.
// The pymss dev environment can be set up with: uv venv .venv && uv pip install -e ../pymss
const PY = process.env.PYMSS_STUDIO_TEST_PYTHON
  || [
    resolve(__dirname, '../.venv/Scripts/python.exe'),
    resolve(__dirname, '../.venv/bin/python'),
  ].find(existsSync)
  || (process.platform === 'win32' ? 'python' : 'python3')
let hasPymssGraph = false
try {
  execFileSync(PY, ['-c', 'import pymss.graph'], { stdio: 'ignore' })
  hasPymssGraph = true
} catch {
  // The adapter unit tests remain available without a pymss development environment.
}

function fixturePath(name) {
  return join(__dirname, 'fixtures/comfy-mss', `${name}.json`)
}

function buildSimpleGraph() {
  const graph = new LGraph()
  // Note: no LGraphCanvas here — serialization does not require rendering, and
  // the canvas constructor starts a render loop that needs DOM Image support.

  const load = LiteGraph.createNode('pymss_load_audio')
  load.pos = [100, 200]
  graph.add(load)

  const params = LiteGraph.createNode('pymss_mss_params')
  params.pos = [100, 400]
  graph.add(params)

  const sep = LiteGraph.createNode('mss_separate')
  sep.pos = [500, 300]
  setSeparateStems(sep, ['vocals', 'other'])
  graph.add(sep)

  const save = LiteGraph.createNode('pymss_save_audio')
  save.pos = [900, 300]
  graph.add(save)

  // Wire: load.audio -> sep.audio ; params.mss_params -> sep.params
  load.connect(0, sep, 0)
  params.connect(0, sep, 1)
  // sep stem outputs: [0]=vocals(Audio),[2]=other(Audio)
  sep.connect(0, save, 0)

  return graph
}

function pymssParse(jsonPaths) {
  // Round-trip through pymss.graph.load_comfy_file in the repo's uv venv.
  const out = execFileSync(
    PY,
    ['-X', 'utf8', '-c', [
      'import sys, pymss.graph as g',
      'for path in sys.argv[1:]:',
      '    d = g.load_comfy_file(path)',
      "    print('OK', len(d.nodes), sorted(n.type for n in d.nodes))",
    ].join('\n'), ...jsonPaths],
    { encoding: 'utf8' },
  ).trim()
  return out.split(/\r?\n/)
}

test('litegraph serialize -> adapter -> pymss load_comfy_file (round-trip)', { skip: !hasPymssGraph }, t => {
  const graph = buildSimpleGraph()
  const serialized = graph.serialize()
  const comfy = adapter.litegraphToComfy(serialized)

  // Structural assertions on the adapter output
  assert.ok(Array.isArray(comfy.nodes) && comfy.nodes.length === 4, 'four nodes serialized')
  assert.ok(Array.isArray(comfy.links) && comfy.links.length === 3, 'three links serialized')
  const types = comfy.nodes.map((n) => n.type).sort()
  assert.deepEqual(types, ['pymss_load_audio', 'pymss_mss_params', 'pymss_mss_separate', 'pymss_save_audio'])

  const sep = comfy.nodes.find((n) => n.type === 'pymss_mss_separate')
  assert.ok(sep, 'separate node present')
  assert.ok(Array.isArray(sep.widgets_values), 'separate node has widgets_values')
  assert.equal(sep.outputs.length, 4, 'separate node has 4 stem outputs (2 stems x audio/string)')

  const dir = mkdtempSync(join(tmpdir(), 'lg-rt-'))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const jsonPath = join(dir, 'wf.json')
  writeFileSync(jsonPath, adapter.toComfyJson(serialized))

  const [result] = pymssParse([jsonPath])
  assert.match(result, /^OK 4 /, 'pymss.graph.load_comfy_file parsed the litegraph output')
  assert.ok(result.includes('mss_separate') && result.includes('pymss_save_audio'))
})

test('adapter preserves editor metadata required by workflow round-trips', () => {
  const source = {
    last_node_id: 0,
    last_link_id: 0,
    nodes: [],
    links: [],
    groups: [{
      id: 1,
      title: 'Stage 1',
      bounding: [0, 0, 320, 180],
      color: '#888888',
      flags: {},
    }],
    config: { alignToGrid: true },
    extra: {
      appDefaults: { device: 'cuda', output_format: 'flac' },
      pymssStudio: { editorVersion: 1 },
    },
  }
  const graph = new LGraph()
  graph.configure(source)
  const converted = adapter.litegraphToComfy(graph.serialize())

  assert.equal(converted.groups?.length, 1)
  assert.equal(converted.groups?.[0]?.title, 'Stage 1')
  assert.deepEqual(converted.groups?.[0]?.bounding, [0, 0, 320, 180])
  assert.deepEqual(converted.config, source.config)
  assert.deepEqual(converted.extra?.appDefaults, source.extra.appDefaults)
  assert.deepEqual(converted.extra?.pymssStudio, source.extra.pymssStudio)
})

test('fixture files (real comfy-mss JSON) still parse through pymss', { skip: !hasPymssGraph }, () => {
  const names = ['example_mss_separate', 'example_ensemble', 'example_vr_separate', 'example_custom_mss_separate', 'example_batch_separate']
  const results = pymssParse(names.map(fixturePath))
  assert.equal(results.length, names.length)
  for (const [index, name] of names.entries()) assert.match(results[index], /^OK /, `${name} parsed`)
})

test('a real comfy-mss workflow file loads into the litegraph editor and round-trips', { skip: !hasPymssGraph }, t => {
  // The user-facing import path: parse a comfy-mss JSON (like testflow.json),
  // configure the litegraph graph with it, serialize back, and confirm the
  // pymss DAG still parses it with all nodes/links intact.
  const source = fixture('example_mss_separate')
  const graph = new LGraph()
  graph.configure({
    nodes: source.nodes,
    links: adapter.comfyLinksToLitegraph(source.links),
    last_node_id: source.last_node_id,
    last_link_id: source.last_link_id,
    groups: source.groups || [],
    version: 1,
  })
  const serialized = graph.serialize()
  const comfy = adapter.litegraphToComfy(serialized)

  assert.equal(comfy.nodes.length, source.nodes.length,
    `all ${source.nodes.length} nodes load into the editor`)
  assert.equal(comfy.links.length, source.links.length,
    `all ${source.links.length} links survive the round-trip`)

  const dir = mkdtempSync(join(tmpdir(), 'lg-tf-'))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const jsonPath = join(dir, 'wf.json')
  writeFileSync(jsonPath, adapter.toComfyJson(serialized))
  const [result] = pymssParse([jsonPath])
  assert.match(result, /^OK /, 'round-tripped graph still parses in pymss')
})
