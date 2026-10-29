import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'

const descriptor = parse(readFileSync(new URL('../src/views/ToolsView.vue', import.meta.url), 'utf8')).descriptor
const source = ts.createSourceFile('tools.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const names = new Set(['applyRequestedPluginView', 'activatePluginTool', 'markUnavailable', 'replaceQuery', 'restoreStoredTool', 'resolveActivePluginHost', 'persistPluginSelection'])
const code = ts.transpileModule(source.statements
  .filter(node => ts.isFunctionDeclaration(node) && names.has(node.name?.text))
  .map(node => node.getText(source)).join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function entry(pluginId, routeId = `${pluginId}/tool`) {
  return { key: `plugin:${routeId}`, routeId, pluginName: pluginId, view: { id: 'tool' } }
}

function selection(query = {}) {
  const requests = []
  const state = {
    route: { query }, activePluginTool: { value: '' }, mountedPluginTool: { value: null }, unavailableToolId: { value: '' },
    switchingDisabled: { value: false }, pluginToolBusy: { value: false }, activePluginHost: { value: null }, restored: { value: false },
    pluginMarket: { snapshot: { value: { pluginsDir: '/plugins' } } }, pluginTools: { value: [entry('convert'), entry('asr'), entry('merge')] },
    router: { replace: value => { requests.push(JSON.parse(JSON.stringify(value))); return Promise.resolve() } }, recordFeatureVisit() {}, updateAudioToolsState() {},
  }
  const controller = vm.runInNewContext(`${code}\n({ applyRequestedPluginView, activatePluginTool, restoreStoredTool, resolveActivePluginHost })`, state)
  return { state, requests, controller }
}

test('requested plugin views activate their plugin-id route entries', () => {
  for (const pluginId of ['pymss.convert', 'pymss.asr', 'pymss.merge']) {
    const { state, controller } = selection({ pluginView: `${pluginId}/tool` })
    state.pluginTools.value = [entry('pymss.convert'), entry('pymss.asr'), entry('pymss.merge')]
    controller.applyRequestedPluginView()
    assert.equal(state.activePluginTool.value, `plugin:${pluginId}/tool`)
  }
})

test('stored selections restore by route id and report a miss so the caller can pick a default', () => {
  const modern = selection()
  assert.equal(modern.controller.restoreStoredTool({ activePluginTool: 'merge/tool' }), true)
  assert.equal(modern.state.activePluginTool.value, 'plugin:merge/tool')
  const stale = selection()
  assert.equal(stale.controller.restoreStoredTool({ activePluginTool: 'removed/tool' }), false)
  assert.equal(stale.state.activePluginTool.value, '')
  assert.equal(stale.state.unavailableToolId.value, '')
})

test('disabled or missing requested views show unavailable state instead of selecting another tool', () => {
  const { state, controller } = selection({ pluginView: 'asr/tool' })
  state.pluginTools.value = [entry('convert')]
  controller.applyRequestedPluginView()
  assert.equal(state.activePluginTool.value, '')
  assert.equal(state.unavailableToolId.value, 'asr/tool')
})

test('busy host preserves its original entry and root even when inventory has the same key', () => {
  const { controller } = selection()
  const original = { entry: entry('convert'), pluginsRoot: '/runtime-a/plugins' }
  const resolved = controller.resolveActivePluginHost([entry('convert')], 'plugin:convert/tool', true, original, '/runtime-b/plugins')
  assert.equal(resolved, original)
})
