import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
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
registerPymssNodes()

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
    assert.deepEqual(actual, expected, locale)
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
    assert.equal(exported.nodes[0].type, type, `preserve type ${type}`)

    const restored = load(exported)
    assert.equal(restored.nodes.length, 1, `restore ${type}`)
    assert.equal(restored.nodes[0].type, type, `restore type ${type}`)
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
  assert.deepEqual(sampleRate.options.values, ['0', '32000', '44100', '48000'])
})

test('new save nodes keep the incoming sample rate instead of resampling to 44.1 kHz', () => {
  const graph = new LGraph()
  const node = LiteGraph.createNode('pymss_save_audio')
  graph.add(node)
  assert.equal(node.widgets.find(widget => widget.name === 'sample_rate').value, '0')
  const exported = exportGraph(graph)
  const values = exported.nodes[0].widgets_values
  assert.deepEqual(values, ['wav', '0', 'FLOAT', 'PCM_24', '320k'])
  // pymss tells this layout from the six-value upstream one by an all-digit widgets_values[1].
  assert.match(values[1], /^\d+$/)
  assert.deepEqual(load(exported).nodes[0].widgets.map(widget => widget.value), values)
})

test('saved workflows keep an explicit 44.1 kHz save rate', () => {
  const source = fixture('example_mss_separate')
  const save = source.nodes.find(node => node.type === 'pymss_save_audio')
  save.widgets_values = ['wav', '44100', 'FLOAT', 'PCM_24', '320k']
  save.inputs = save.inputs.filter(input => input.name !== 'output_folder')
  const exported = exportGraph(load(source))
  assert.deepEqual(exported.nodes.find(n => n.id === save.id).widgets_values, ['wav', '44100', 'FLOAT', 'PCM_24', '320k'])
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
