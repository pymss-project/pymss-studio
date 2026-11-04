import assert from 'node:assert/strict'
import test from 'node:test'
import { normalizePluginCategory, pluginLocale, pluginSearchText, pluginViewTitle } from '../src/features/plugin-marketplace/presentation.ts'

test('plugin locales follow the UI language and retain English fallback operation names', () => {
  const values = {
    en: { title: 'Speech recognition', description: 'Speech to text', operations: { transcribe: 'Transcribe', inspect: 'Inspect' } },
    'zh-CN': { title: '语音识别', operations: { transcribe: '语音转写' } },
  }
  assert.deepEqual(pluginLocale(values, 'zh-Hans'), { title: '语音识别', description: 'Speech to text', operations: { transcribe: '语音转写', inspect: 'Inspect' } })
  assert.equal(pluginLocale(values, 'en-US').title, 'Speech recognition')
  assert.equal(pluginLocale(values, 'de-DE').title, 'Speech recognition')
  assert.deepEqual(pluginLocale(undefined, 'zh-CN'), { operations: {} })
})

test('category names come from recognized metadata and unknown values stay unclassified', () => {
  for (const category of ['convert', 'analyze', 'recognize', 'edit']) assert.equal(normalizePluginCategory(category), category)
  for (const value of ['asr', 'analysis', undefined, [], {}, 3]) assert.equal(normalizePluginCategory(value), null)
})

test('plugin view names use the main title or operation translation independently of execution availability', () => {
  const plugin = { locales: {
    en: { title: 'Audio analysis', operations: { 'analysis.spectrum': 'Spectrum analysis' } },
    'zh-CN': { title: '音频分析', operations: { 'analysis.spectrum': '频谱分析' } },
  } }
  assert.equal(pluginViewTitle(plugin, { id: 'tool', title: 'Audio Analysis' }, 'zh-CN'), '音频分析')
  assert.equal(pluginViewTitle(plugin, { id: 'spectrum', title: 'Spectrum analysis', operation: 'analysis.spectrum' }, 'zh-CN'), '频谱分析')
  assert.equal(pluginViewTitle(plugin, { id: 'spectrum', title: 'Spectrum analysis', operation: 'analysis.spectrum' }, 'en-US'), 'Spectrum analysis')
  assert.equal(pluginViewTitle(plugin, { id: 'custom', title: 'Custom view' }, 'zh-CN'), 'Custom view')
})

test('catalog searches include package identifiers and both localized descriptions', () => {
  const text = pluginSearchText({ name: 'asr', tags: ['speech'], locales: {
    en: { title: 'Speech recognition' }, 'zh-CN': { title: '语音识别', description: '生成字幕', tags: ['转写'] },
  } })
  for (const query of ['asr', 'speech recognition', '语音识别', '字幕', '转写']) assert.ok(text.includes(query))
})
