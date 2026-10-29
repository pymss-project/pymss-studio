import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'
import { ref, reactive, computed } from 'vue'

const descriptor = parse(readFileSync(new URL('../src/components/debug/PluginSourcePanel.vue', import.meta.url), 'utf8')).descriptor
const source = ts.createSourceFile('source-panel.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const code = ts.transpileModule(source.statements.filter(node => !ts.isImportDeclaration(node)).map(node => node.getText(source)).join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
const localInfo = () => ({ defaults: { mode: 'local', localPath: '/plugins', repositoryUrl: 'https://github.com/example/plugins', ref: 'main', registryRef: 'main' }, effective: { mode: 'local', localPath: '/plugins', origin: 'environment' }, available: true, error: null, registryUrl: 'file:///plugins/registry.json', catalogCount: 2 })
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const turn = () => new Promise(setImmediate)
function panel(overrides = {}) {
  const calls = [], lifecycle = {}, dialogs = []
  const market = {
    snapshot: ref({ runtime: { identity: 'cpu-runtime', pythonPath: '/runtime/bin/python' }, installed: [{ name: 'alpha', managed: true, enabled: false }], preinstalled: [{ name: 'alpha' }, { name: 'beta' }], catalog: [{ name: 'alpha' }, { name: 'beta' }] }),
    busy: ref(false), loading: ref(false), catalogLoading: ref(false), logs: ref([]), error: ref(''),
    refresh: async () => calls.push(['catalog']), manage: async (...args) => calls.push(['manage', ...args]), cancel: async () => calls.push(['cancel']),
    ...overrides.market,
  }
  const invoke = overrides.invoke || (async (command, args) => { calls.push([command, args]); return localInfo() })
  const scope = { ref, reactive, computed, console, invoke, open: async () => '/picked',
    useI18n: () => ({ t: key => key, locale: ref('zh-CN') }),
    useDialog: () => ({ warning: options => { dialogs.push(options); return { destroy() {} } } }),
    useMessage: () => ({ success: text => calls.push(['success', text]) }),
    usePluginMarketplace: () => market,
    onMounted: callback => { lifecycle.mount = callback }, onBeforeUnmount: callback => { lifecycle.unmount = callback },
  }
  const controller = vm.runInNewContext(`${code}\n({ info, draft, dirty, busy, confirming, acquiring, error, selection, catalog, completed, loadSource, readCatalog, saveSource, acquire, confirmAcquisition, cancelAcquisition })`, scope)
  return { controller, calls, dialogs, lifecycle, market }
}

test('local defaults retain remote fields and unsaved source changes cannot fetch a catalog', async () => {
  const { controller: c, calls } = panel()
  await c.loadSource(); await c.readCatalog()
  assert.equal(c.draft.localPath, '/plugins')
  c.draft.mode = 'repository'
  assert.equal(c.draft.repositoryUrl, 'https://github.com/example/plugins')
  assert.equal(c.dirty.value, true)
  const count = calls.length
  await c.readCatalog()
  assert.equal(calls.length, count)
  await c.saveSource()
  const saved = calls.find(([name]) => name === 'debug_save_plugin_source')[1].source
  assert.equal(saved.mode, 'repository')
  assert.equal(saved.registryRef, 'main')
})

test('a failed save keeps the editable draft and does not acquire from a stale source', async () => {
  const { controller: c, calls } = panel({ invoke: async command => {
    calls.push([command])
    if (command === 'debug_save_plugin_source') throw new Error('Invalid registry')
    return localInfo()
  } })
  await c.loadSource()
  c.draft.localPath = '/missing'
  await c.saveSource()
  assert.match(c.error.value, /Invalid registry/)
  assert.equal(c.draft.localPath, '/missing')
  assert.equal(c.dirty.value, true)
  assert.equal(calls.some(([name]) => name === 'catalog'), false)
})

test('reset clears the override rather than persisting a machine-specific default', async () => {
  const { controller: c, calls } = panel()
  await c.loadSource(); await c.saveSource(true)
  assert.equal(calls.find(([name]) => name === 'debug_save_plugin_source')[1].source, null)
})

test('a machine-local override resets to the remote project default', async () => {
  const defaults = { ...localInfo().defaults, mode: 'repository', localPath: null }
  const { controller: c } = panel({ invoke: async command => command === 'debug_save_plugin_source'
    ? { ...localInfo(), defaults, override: null, effective: { ...defaults, origin: 'manifest' } }
    : { ...localInfo(), defaults, override: { mode: 'local', localPath: '/plugins' }, effective: { mode: 'local', localPath: '/plugins', origin: 'debug' } } })
  await c.loadSource()
  assert.equal(c.draft.mode, 'local')
  await c.saveSource(true)
  assert.equal(c.draft.mode, 'repository')
  assert.equal(c.draft.localPath, '')
  assert.equal(c.draft.repositoryUrl, 'https://github.com/example/plugins')
})

test('late source reads cannot update an unmounted panel', async () => {
  const pending = deferred()
  const { controller: c, lifecycle } = panel({ invoke: () => pending.promise })
  const loading = c.loadSource()
  lifecycle.unmount(); pending.resolve(localInfo()); await loading
  assert.equal(c.info.value, null)
})

test('dismissing any acquisition confirmation leaves no pending batch', async () => {
  for (const event of ['onNegativeClick', 'onEsc', 'onClose', 'onMaskClick']) {
    const { controller: c, dialogs, calls } = panel()
    await c.loadSource(); await c.readCatalog(); c.confirmAcquisition()
    assert.equal(c.confirming.value, true)
    dialogs[0][event]()
    assert.equal(c.confirming.value, false)
    assert.equal(calls.some(([name]) => name === 'manage'), false)
  }
})

test('batches update installed plugins, install new ones, and stop on source replacement', async () => {
  const { controller: c, calls } = panel()
  await c.loadSource(); await c.readCatalog(); await c.acquire(['alpha', 'beta'])
  assert.deepEqual(calls.filter(([name]) => name === 'manage').map(([, action, options]) => [action, options.name]), [['update', 'alpha'], ['install', 'beta']])
  assert.equal(calls.find(([name]) => name === 'manage')[2].expectedRuntime, 'cpu-runtime')
  const changed = panel({ invoke: async () => ({ ...localInfo(), effective: { ...localInfo().effective, localPath: '/other' } }) })
  changed.controller.info.value = localInfo()
  await changed.controller.readCatalog(); await changed.controller.acquire(['alpha'])
  assert.match(changed.controller.error.value, /sourceChanged/)
  assert.equal(changed.calls.some(([name]) => name === 'manage'), false)
})

test('leaving or cancelling a pending batch never starts its next plugin', async () => {
  for (const leave of [false, true]) {
    const pending = deferred(), operations = []
    const { controller: c, lifecycle } = panel({ market: { manage: async (...args) => { operations.push(args); await pending.promise } } })
    await c.loadSource(); await c.readCatalog()
    const running = c.acquire(['alpha', 'beta'])
    await turn()
    if (leave) lifecycle.unmount(); else await c.cancelAcquisition()
    pending.resolve(); await running
    assert.equal(operations.length, 1)
  }
})
