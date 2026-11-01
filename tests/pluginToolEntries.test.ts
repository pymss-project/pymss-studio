import assert from 'node:assert/strict'
import test from 'node:test'
import { pluginToolEntries, pluginToolKey, pluginToolCategories } from '../src/features/plugin-marketplace/toolEntries.ts'
import { pluginLocale } from '../src/features/plugin-marketplace/presentation.ts'

const view = (id: string, extra = {}) => ({
  id, title: id, entry: 'ui/index.html', placements: ['tool'], permissions: ['operations'], ...extra,
})

test('operation views use their own translated name without reusing the main tool title', () => {
  const entries = pluginToolEntries([{ name: 'analysis', managed: true, loaded: true,
    locales: {
      en: { title: 'Audio analysis', operations: { 'pymss.analysis.spectrum': 'Spectrum analysis' } },
      'zh-CN': { title: '音频分析', operations: { 'pymss.analysis.spectrum': '频谱分析' } },
      de: { title: 'Audioanalyse' },
    }, contributions: { views: [view('tool'), view('spectrum', { operation: 'pymss.analysis.spectrum' }), view('extra')] },
  }])
  assert.equal(pluginLocale(entries[0].locales, 'zh-CN').title, '音频分析')
  assert.equal(pluginLocale(entries[1].locales, 'zh-CN').title, '频谱分析')
  assert.equal(pluginLocale(entries[1].locales, 'en').title, 'Spectrum analysis')
  assert.equal(pluginLocale(entries[1].locales, 'fr').title, 'Spectrum analysis')
  assert.equal(pluginLocale(entries[1].locales, 'de').title, 'Spectrum analysis')
  assert.equal(entries[2].locales, undefined)
})

test('plugin tool entries require managed, loaded, enabled views and are keyed by plugin id', () => {
  const entries = pluginToolEntries([
    { name: 'pymss.convert', managed: true, loaded: true, enabled: true, toolCategory: 'convert', toolIcon: 'SwapHorizontalOutline', contributions: { views: [view('tool')] } },
    { name: 'pymss.asr', managed: true, loaded: true, enabled: true, toolCategory: 'recognize', contributions: { views: [view('tool', { icon: 'MicOutline' })] } },
    { name: 'author.disabled', managed: true, loaded: true, enabled: false, contributions: { views: [view('tool')] } },
    { name: 'author.manual', managed: false, loaded: true, contributions: { views: [view('tool')] } },
  ])
  assert.deepEqual(entries.map(entry => [entry.key, entry.routeId, entry.category, entry.icon]), [
    [pluginToolKey('pymss.convert', 'tool'), 'pymss.convert/tool', 'convert', 'SwapHorizontalOutline'],
    [pluginToolKey('pymss.asr', 'tool'), 'pymss.asr/tool', 'recognize', 'MicOutline'],
  ])
})

test('view metadata overrides plugin metadata', () => {
  const [entry] = pluginToolEntries([
    { name: 'pymss.merge', managed: true, loaded: true, toolCategory: 'edit', toolIcon: 'GitMergeOutline', contributions: { views: [view('panel', { category: 'analyze', icon: 'SearchOutline' })] } },
  ])
  assert.equal(entry.category, 'analyze')
  assert.equal(entry.icon, 'SearchOutline')
})

test('unprepared tools keep their entry and source choices without becoming a runnable view', () => {
  const bindings = [{ runtime: 'cpu-py312', version: '1.0.0', content: null }]
  const [entry] = pluginToolEntries([
    { name: 'hello', managed: true, loaded: false, enabled: true, availability: 'not_prepared',
      availableBindings: bindings, sourceSelectionRequired: true, contributions: { views: [view('tool')] } },
    { name: 'legacy.failed', managed: true, loaded: false, contributions: { views: [view('tool')] } },
  ])
  assert.equal(entry.routeId, 'hello/tool')
  assert.equal(entry.availability, 'not_prepared')
  assert.deepEqual(entry.availableBindings, bindings)
  assert.equal(entry.sourceSelectionRequired, true)
})

test('unknown tool categories remain visible in the ungrouped section', () => {
  const [entry] = pluginToolEntries([
    { name: 'custom', managed: true, loaded: true, contributions: { views: [view('extra', { category: 'custom-category' })] } },
  ])
  assert.equal(entry.key, 'plugin:custom/extra')
  assert.equal(entry.category, null)
})

test('marketplace categories match tool views, retain disabled metadata and fall back from invalid view categories', () => {
  const plugin = { name: 'mixed', managed: true, loaded: false, enabled: false, toolCategory: 'edit', contributions: { views: [
    view('analysis', { category: 'analyze' }), view('editing', { category: 'invalid' }),
  ] } }
  assert.deepEqual(pluginToolCategories(plugin), ['analyze', 'edit'])
  assert.deepEqual(pluginToolEntries([{ ...plugin, loaded: true, enabled: true }]).map(item => item.category), ['analyze', 'edit'])
  assert.deepEqual(pluginToolCategories({}), [null])
})
