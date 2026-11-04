import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'
import { computed, effectScope, ref, watch } from 'vue'
import { pluginLocale, pluginSearchText, pluginViewTitle } from '../src/features/plugin-marketplace/presentation.ts'
import { canApplyPluginUpdate, canCheckPluginUpdate, pluginUpdateKey, pluginUpdateState } from '../src/features/plugin-marketplace/updates.ts'
import { buildPluginLibrary, installedPluginKey, pluginNeedsAttention } from '../src/features/plugin-marketplace/library.ts'

const script = parse(readFileSync(new URL('../src/views/PluginsView.vue', import.meta.url), 'utf8')).descriptor.scriptSetup.content
const source = ts.createSourceFile('plugins.ts', script, ts.ScriptTarget.Latest, true)
const code = ts.transpileModule(source.statements.filter(node => !ts.isImportDeclaration(node)).map(node => node.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function page(t) {
  const calls = [], confirmations = [], routes = [], nativeCalls = [], errors = [], refreshes = []
  const plugin = { name: 'analysis', managed: true, loaded: true, enabled: true, prepared: true, url: 'https://example.org/analysis',
    path: '/plugins/analysis', locales: { 'zh-CN': { title: '音频分析', operations: { spectrum: '频谱分析' } } },
    contributions: { views: [{ id: 'tool', title: 'Audio analysis', placements: ['tool'] }, { id: 'spectrum', title: 'Spectrum analysis', operation: 'spectrum', placements: ['tool'] }] },
  }
  const market = { snapshot: ref({ installed: [plugin], catalog: [], runtime: { identity: 'cpu', pythonPath: '/cpu/python' } }),
    updates: ref({}), loading: ref(false), catalogLoading: ref(false), error: ref(''), busy: ref(false), exclusiveBusy: ref(false),
    logs: ref([]), activities: ref([]), activity: ref(null), openMarketplace: async (...args) => { refreshes.push(args) }, refreshInventory() {}, cancel() {},
    manage: async (...args) => { calls.push(args) },
    clearUpdateChecks: () => { market.updates.value = {} },
  }
  const settings = { pluginSource: 'auto', pluginViewMode: 'list', savePluginSourcePreference: async value => { settings.pluginSource = value } }
  const scope = effectScope()
  t.after(() => scope.stop())
  const globals = { computed, effectScope, ref, watch, onMounted() {}, onBeforeUnmount() {},
    useI18n: () => ({ t: key => key, locale: ref('zh-CN') }), usePluginMarketplace: () => market,
    useRouter: () => ({ push: route => routes.push(route) }), useAppStore: () => ({}), useSettingsStore: () => settings,
    useDialog: () => ({ warning: value => confirmations.push(value), info: value => confirmations.push(value) }), useMessage: () => ({ success() {}, warning() {}, error: value => errors.push(value) }),
    invoke: async (...args) => { nativeCalls.push(args) },
    pluginLocale, pluginSearchText, pluginViewTitle, pluginCategories: [], normalizePluginCategory: value => value,
    canApplyPluginUpdate, canCheckPluginUpdate, pluginUpdateKey, pluginUpdateState,
    buildPluginLibrary, installedPluginKey, pluginNeedsAttention,
    pluginToolCategories: () => ['analyze'], h() {}, NIcon: {},
    ...Object.fromEntries(['OpenOutline', 'FolderOpenOutline', 'PowerOutline', 'BuildOutline', 'TrashOutline'].map(name => [name, {}])),
  }
  const controller = scope.run(() => vm.runInNewContext(`${code}\n({ statusFilter, category, search, libraryEntries, visibleEntries, recentInstalls, attentionCount, activeTasks, activePluginTask, openPluginLogs, logTaskId, showLogs, changePluginSource, refreshLibrary, restoreBundled, install, pluginMenu, pluginMenuOptions, moreOptions, openPluginViews, openPluginContextMenu, openPluginKeyboardMenu, closePluginMenuOnKeydown, selectPluginMenu, detailEntry, detailKey, showDetails, openEntryDetails, closeEntryDetails, removedPreinstalled })`, globals))
  function event(overrides = {}) {
    const element = { isConnected: true, focused: false, focus() { this.focused = true }, closest: () => null,
      getBoundingClientRect: () => ({ left: 40, bottom: 100 }) }
    return { target: element, currentTarget: element, clientX: 60, clientY: 80, preventDefault() {}, stopPropagation() {}, ...overrides }
  }
  return { controller, market, settings, calls, confirmations, routes, nativeCalls, errors, globals, refreshes, event, plugin: () => market.snapshot.value.installed[0] }
}

test('changing an inactive official preference keeps custom catalog, checks and installed placement', async t => {
  const { controller: c, market, settings, refreshes } = page(t)
  market.snapshot.value.catalogSource = { supported: true, provider: 'custom' }
  market.updates.value.analysis = { status: 'latest' }
  c.recentInstalls.value = new Map([['managed:analysis', 'Analysis']])
  await c.changePluginSource('github')
  assert.equal(settings.pluginSource, 'github')
  assert.equal(market.updates.value.analysis.status, 'latest')
  assert.equal(c.recentInstalls.value.size, 1)
  assert.equal(refreshes.length, 0)
})

test('changing an effective official preference invalidates checks and refreshes the catalog', async t => {
  const { controller: c, market, settings, refreshes } = page(t)
  market.snapshot.value.catalogSource = { supported: true, provider: 'github' }
  market.updates.value.analysis = { status: 'latest' }
  await c.changePluginSource('gitcode')
  assert.equal(settings.pluginSource, 'gitcode')
  assert.equal(market.updates.value.analysis, undefined)
  assert.equal(refreshes.length, 1)
})

test('running task rows belong to the managed runtime and open their own log', t => {
  const { controller: c, market, plugin } = page(t)
  market.activities.value = [
    { id: 'previous', name: 'analysis', runtime: 'cpu', action: 'repair', phase: 'resolving', status: 'failed' },
    { id: 'current', name: 'analysis', runtime: 'cpu', action: 'repair', phase: 'resolving', status: 'running' },
    { id: 'foreign', name: 'analysis', runtime: 'cuda', action: 'install', phase: 'installing', status: 'running' },
  ]
  assert.equal(c.activeTasks.value.length, 2)
  assert.equal(c.activePluginTask(plugin()).id, 'current')
  assert.equal(c.activePluginTask({ ...plugin(), managed: false }), undefined)
  c.openPluginLogs('current')
  assert.equal(c.logTaskId.value, 'current')
  assert.equal(c.showLogs.value, true)
  market.activities.value[1].status = 'succeeded'
  assert.equal(c.activePluginTask(plugin()), undefined)
})

test('list and card details resolve actual installation keys and close when the runtime changes', t => {
  const { controller: c, market, settings, plugin } = page(t)
  market.snapshot.value.installed.push({ ...plugin(), managed: false, path: '/manual/analysis' })
  for (const mode of ['list', 'card']) {
    settings.pluginViewMode = mode
    for (const row of market.snapshot.value.installed) {
      c.openEntryDetails(installedPluginKey(row))
      assert.equal(c.showDetails.value, true)
      assert.strictEqual(c.detailEntry.value.installed, row)
    }
  }
  market.snapshot.value.runtime.identity = 'cuda'
  assert.equal(c.showDetails.value, false)
  c.closeEntryDetails()
  assert.equal(c.detailKey.value, '')
  c.openEntryDetails('removed-entry')
  assert.equal(c.showDetails.value, false)
})

test('a delayed details close cannot clear a newly opened plugin', t => {
  const { controller: c, plugin } = page(t)
  c.openEntryDetails(installedPluginKey(plugin()))
  c.showDetails.value = false
  c.openEntryDetails(installedPluginKey(plugin()))
  c.closeEntryDetails()
  assert.equal(c.showDetails.value, true)
  assert.equal(c.detailEntry.value.name, plugin().name)
})

test('multiple views use a localized picker while folder access belongs to the context menu', t => {
  const { controller: c, plugin, event, routes } = page(t)
  c.openPluginViews(event(), plugin())
  assert.equal(c.pluginMenu.value.mode, 'views')
  assert.deepEqual(Array.from(c.pluginMenuOptions.value, item => item.label), ['音频分析', '频谱分析'])
  c.selectPluginMenu('view:spectrum')
  assert.equal(routes[0].query.pluginView, 'analysis/spectrum')
  assert.equal(c.pluginMenu.value, null)
  c.openPluginContextMenu(event(), plugin())
  const contextKeys = Array.from(c.pluginMenuOptions.value, item => item.key)
  assert.deepEqual(contextKeys, ['open_directory', 'directory-divider', 'disable', 'repair', 'divider', 'uninstall'])
})

test('runtime changes, inventory replacement and filters dismiss the old action target', t => {
  const { controller: c, plugin, event, market, confirmations } = page(t)
  for (const change of [() => { market.snapshot.value.runtime.identity = 'cuda' },
    () => { market.snapshot.value = { ...market.snapshot.value } }, () => { c.search.value = 'other' }]) {
    c.openPluginContextMenu(event(), plugin())
    change()
    assert.equal(c.pluginMenu.value, null)
    c.selectPluginMenu('uninstall')
  }
  assert.equal(confirmations.length, 0)
})

test('removing and readding an inventory entry cannot reopen its old menu', t => {
  const { controller: c, plugin, event, market } = page(t)
  c.openPluginContextMenu(event(), plugin())
  const previous = market.snapshot.value.installed.pop()
  assert.equal(c.pluginMenu.value, null)
  market.snapshot.value.installed.push(previous)
  assert.equal(c.pluginMenu.value, null)
})

test('dispatch rechecks disabled actions and read-only plugins only allow folder access', t => {
  const { controller: c, plugin, event, market, calls, confirmations } = page(t)
  c.openPluginContextMenu(event(), plugin())
  market.busy.value = true
  c.selectPluginMenu('uninstall')
  assert.equal(confirmations.length, 0)
  market.busy.value = false
  plugin().managed = false
  assert.deepEqual(Array.from(c.moreOptions(plugin()), item => item.key), ['open_directory'])
  c.openPluginContextMenu(event(), plugin())
  assert.ok(c.pluginMenu.value)
  c.selectPluginMenu('disable')
  assert.equal(calls.length, 0)
})

test('read-only plugins without a folder leave the native context menu available', t => {
  const { controller: c, plugin, event } = page(t)
  plugin().managed = false
  plugin().contributions.views = []
  plugin().path = ''
  let prevented = false
  c.openPluginContextMenu(event({ preventDefault() { prevented = true } }), plugin())
  assert.equal(c.moreOptions(plugin()).length, 0)
  assert.equal(c.pluginMenu.value, null)
  assert.equal(prevented, false)
  c.openPluginKeyboardMenu(event({ key: 'ContextMenu', preventDefault() { prevented = true } }), plugin())
  assert.equal(c.pluginMenu.value, null)
  assert.equal(prevented, false)
})

test('opening the plugin folder uses the selected installation and rejects a stale runtime target', async t => {
  const { controller: c, plugin, event, nativeCalls, market } = page(t)
  c.openPluginContextMenu(event(), plugin())
  await c.selectPluginMenu('open_directory')
  assert.deepEqual(nativeCalls.map(([command, args]) => [command, args.path]), [['reveal_path', '/plugins/analysis']])
  c.openPluginContextMenu(event(), plugin())
  market.snapshot.value.runtime.pythonPath = '/other/python'
  await c.selectPluginMenu('open_directory')
  assert.equal(nativeCalls.length, 1)
})

test('folder failures surface without dispatching a plugin modification', async t => {
  const { controller: c, plugin, event, globals, errors, calls } = page(t)
  globals.invoke = async () => { throw new Error('Folder unavailable') }
  c.openPluginContextMenu(event(), plugin())
  await c.selectPluginMenu('open_directory')
  assert.equal(errors.length, 1)
  assert.match(errors[0], /Folder unavailable/)
  assert.equal(calls.length, 0)
})

test('keyboard menus restore focus on Escape and single-view plugins open directly', t => {
  const { controller: c, plugin, event, routes } = page(t)
  const keyboard = event({ key: 'F10', shiftKey: true })
  c.openPluginKeyboardMenu(keyboard, plugin())
  assert.ok(c.pluginMenu.value)
  c.closePluginMenuOnKeydown(event({ key: 'Escape' }))
  assert.equal(c.pluginMenu.value, null)
  assert.equal(keyboard.currentTarget.focused, true)
  plugin().contributions.views.splice(1)
  c.openPluginViews(event(), plugin())
  assert.equal(routes[0].query.pluginView, 'analysis/tool')
  assert.equal(c.pluginMenu.value, null)
})

test('removed defaults exclude not-yet-prepared and pending-removal entries and honor localized category searches', t => {
  const { controller: c, market } = page(t)
  market.snapshot.value.preinstalled = ['removed', 'new', 'pending'].map(name => ({ name, installed: false, version: '1.0' }))
  market.snapshot.value.preinstalledAvailable = ['new']
  market.snapshot.value.installed.push({ name: 'pending', managed: true, removalPending: true })
  market.snapshot.value.catalog.push({ name: 'removed', category: 'edit', locales: { 'zh-CN': { title: '音频转换' } } })
  c.search.value = '音频'
  c.category.value = 'edit'
  assert.deepEqual(Array.from(c.visibleEntries.value, item => item.name), ['removed'])
  c.category.value = 'analyze'
  assert.equal(c.visibleEntries.value.some(entry => entry.name === 'removed'), false)
})

test('successful installs keep their list position until a refresh or status filter change', async t => {
  const { controller: c, market, confirmations, refreshes, calls } = page(t)
  market.snapshot.value.catalog = [{ name: 'beta', url: '/source/beta', tags: [], description: 'Beta' }]
  const available = () => c.visibleEntries.value.find(entry => entry.name === 'beta')
  const key = available().key
  c.install('beta')
  confirmations[0].onPositiveClick()
  await Promise.resolve()
  assert.equal(calls[0][0], 'install')
  market.snapshot.value = { ...market.snapshot.value, installed: [...market.snapshot.value.installed,
    { name: 'beta', managed: true, loaded: true, url: '/source/beta', contributions: { views: [] } }] }
  assert.equal(available().key, key)
  assert.equal(available().installed.name, 'beta')
  c.statusFilter.value = 'installed'
  assert.equal(c.visibleEntries.value.some(entry => entry.name === 'beta'), true)
  c.statusFilter.value = 'all'
  assert.equal(c.recentInstalls.value.size, 0)
  await c.refreshLibrary()
  assert.equal(refreshes.length, 1)
  assert.equal(c.visibleEntries.value.some(entry => entry.name === 'beta'), true)
})

test('a different installed title cannot reorder the available row after installation', async t => {
  const { controller: c, market, confirmations } = page(t)
  market.snapshot.value.catalog = [
    { name: 'new', url: '/source/new', tags: [], locales: { 'zh-CN': { title: 'Zulu' } } },
    { name: 'other', url: '/source/other', tags: [], locales: { 'zh-CN': { title: 'Mike' } } },
  ]
  const names = () => Array.from(c.visibleEntries.value, entry => entry.name)
  assert.deepEqual(names(), ['analysis', 'other', 'new'])
  c.install('new')
  confirmations[0].onPositiveClick()
  await Promise.resolve()
  market.snapshot.value.installed.push({ name: 'new', managed: true, loaded: true, url: '/source/new', locales: { 'zh-CN': { title: 'Alpha' } }, contributions: { views: [] } })
  assert.deepEqual(names(), ['analysis', 'other', 'new'])
})

test('restoring an existing unprepared bundle keeps its installed position', async t => {
  const { controller: c, plugin, calls } = page(t)
  plugin().prepared = false
  plugin().preinstalled = true
  await c.restoreBundled(plugin().name, installedPluginKey(plugin()))
  assert.equal(c.recentInstalls.value.size, 0)
  assert.equal(c.visibleEntries.value[0].name, plugin().name)
  assert.equal(calls[0][0], 'install_preinstalled')
})

test('other catalog sources never add duplicate install rows or dispatch an overwrite', t => {
  const { controller: c, market, confirmations, calls, plugin, event } = page(t)
  market.snapshot.value.catalog = [{ name: 'analysis', url: 'https://other.example/plugins', description: 'Other', tags: [] }]
  c.install('analysis')
  assert.equal(confirmations.length, 0)
  assert.equal(calls.length, 0)
  c.openPluginContextMenu(event(), plugin())
  assert.equal(c.visibleEntries.value.length, 1)
  assert.equal(c.visibleEntries.value[0].otherSources.length, 1)
  c.statusFilter.value = 'available'
  assert.equal(c.pluginMenu.value, null)
  market.snapshot.value = { ...market.snapshot.value }
  assert.equal(c.visibleEntries.value.length, 0)
})

test('an active attention filter stays clearable after the last issue is resolved', t => {
  const { controller: c, plugin } = page(t)
  plugin().prepared = false
  assert.equal(c.attentionCount.value, 1)
  c.statusFilter.value = 'attention'
  plugin().prepared = true
  assert.equal(c.statusFilter.value, 'attention')
  assert.equal(c.attentionCount.value, 0)
  assert.equal(c.visibleEntries.value.length, 0)
  c.statusFilter.value = 'all'
  assert.equal(c.visibleEntries.value.length, 1)
})

test('the single status filter combines with search and category without duplicating installed sources', t => {
  const { controller: c, market } = page(t)
  market.snapshot.value.catalog = [
    { name: 'analysis', url: 'https://different.example/source', description: 'Different source', tags: [] },
    { name: 'available', url: '/new/source', category: 'edit', description: 'Audio editor', tags: [] },
  ]
  assert.deepEqual(Array.from(c.visibleEntries.value, entry => entry.name), ['analysis', 'available'])
  c.statusFilter.value = 'available'
  assert.deepEqual(Array.from(c.visibleEntries.value, entry => entry.name), ['available'])
  c.category.value = 'edit'
  c.search.value = 'audio'
  assert.equal(c.visibleEntries.value.length, 1)
  c.category.value = 'analyze'
  assert.equal(c.visibleEntries.value.length, 0)
  c.category.value = 'all'
  c.search.value = ''
  c.statusFilter.value = 'installed'
  assert.deepEqual(Array.from(c.visibleEntries.value, entry => entry.name), ['analysis'])
})
