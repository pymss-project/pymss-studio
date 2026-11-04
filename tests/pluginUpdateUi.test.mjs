import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'
import { computed, ref } from 'vue'
import { canApplyPluginUpdate, canCheckPluginUpdate, pluginUpdateKey, pluginUpdateState } from '../src/features/plugin-marketplace/updates.ts'

const descriptor = parse(readFileSync(new URL('../src/views/PluginsView.vue', import.meta.url), 'utf8')).descriptor
const source = ts.createSourceFile('plugins.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const code = ts.transpileModule(source.statements.filter(node => !ts.isImportDeclaration(node)).map(node => node.getText(source)).join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function page() {
  const calls = [], warnings = [], confirmations = []
  const market = {
    snapshot: ref({ runtime: { identity: 'cpu', pythonPath: '/cpu/python' }, catalog: [], installed: [{ name: 'hello', version: '1.0', managed: true, url: '/source', contentDigest: 'installed' }] }),
    updates: ref({ hello: { status: 'update', remote_version: '2.0', target: { schemaVersion: 1, candidate: { version: '2.0', commit: 'a'.repeat(40) } } } }), loading: ref(false), catalogLoading: ref(false), error: ref(''), busy: ref(false), exclusiveBusy: ref(false), logs: ref([]), activity: ref(null), activities: ref([]),
    openMarketplace() {}, refresh() {}, cancel() {}, manage: async (...args) => { calls.push(args) },
  }
  const scope = { computed, ref, nextTick: async callback => callback?.(), watch() {}, onMounted() {}, onBeforeUnmount() {},
    canApplyPluginUpdate, canCheckPluginUpdate, pluginUpdateKey, pluginUpdateState,
    useI18n: () => ({ t: key => key, locale: ref('zh-CN') }), usePluginMarketplace: () => market,
    useRouter: () => ({ push() {} }), useAppStore: () => ({}), useSettingsStore: () => ({ pluginDefaultsPolicy: 'review' }),
    useDialog: () => ({ warning: options => confirmations.push(options) }),
    useMessage: () => ({ success() {}, warning: value => warnings.push(value), error() {} }),
  }
  const controller = vm.runInNewContext(`${code}\n({ selectMore, confirmUpdate, showUpdate, installDependencies, updateRuntime, showLogs, installDisabled })`, scope)
  return { controller, calls, warnings, confirmations, market }
}

test('plugin updates default to preserving dependencies and bind the reviewed runtime', async () => {
  const { controller: c, calls } = page()
  c.selectMore('update', { name: 'hello' })
  assert.equal(c.showUpdate.value, true)
  assert.equal(c.installDependencies.value, false)
  await c.confirmUpdate()
  assert.equal(c.showUpdate.value, false)
  assert.equal(c.showLogs.value, false)
  assert.equal(calls[0][0], 'update')
  assert.equal(calls[0][1].installDependencies, false)
  assert.equal(calls[0][1].expectedRuntime, 'cpu')
  assert.equal(calls[0][1].expectedUpdate.target.candidate.commit, 'a'.repeat(40))
})

test('dependency installation is explicit and is not remembered for the next update', async () => {
  const { controller: c, calls } = page()
  c.selectMore('update', { name: 'hello' })
  c.installDependencies.value = true
  await c.confirmUpdate()
  assert.equal(calls[0][1].installDependencies, true)
  c.selectMore('update', { name: 'hello' })
  assert.equal(c.installDependencies.value, false)
})

test('runtime replacement rejects a stale update confirmation before starting work', async () => {
  const { controller: c, calls, market, warnings } = page()
  c.selectMore('update', { name: 'hello' })
  market.snapshot.value.runtime.identity = 'cuda'
  await c.confirmUpdate()
  assert.equal(calls.length, 0)
  assert.equal(warnings[0], 'plugins.updateRuntimeChanged')
})

test('a changed installation or source rejects the open update dialog', async () => {
  for (const patch of [{ contentDigest: 'changed' }, { url: '/other-source' }]) {
    const { controller: c, calls, market, warnings } = page()
    c.selectMore('update', { name: 'hello' })
    Object.assign(market.snapshot.value.installed[0], patch)
    await c.confirmUpdate()
    assert.equal(calls.length, 0)
    assert.equal(warnings[0], 'plugins.updateInstallationChanged')
  }
})

test('the confirmation keeps the reviewed target and manual re-fetch is a separate action', async () => {
  const { controller: c, calls, market } = page()
  c.selectMore('update', { name: 'hello' })
  market.updates.value.hello.target.candidate.commit = 'b'.repeat(40)
  await c.confirmUpdate()
  assert.equal(calls[0][1].expectedUpdate.target.candidate.commit, 'a'.repeat(40))
  market.updates.value = {}
  c.selectMore('refetch', { name: 'hello' })
  assert.equal(c.showUpdate.value, true)
  await c.confirmUpdate()
  assert.equal('expectedUpdate' in calls[1][1], false)
  c.selectMore('update', { name: 'hello' })
  assert.equal(c.showUpdate.value, false)
})

test('a preceding read-only copy never intercepts a managed update or reinstall', async () => {
  for (const action of ['update', 'refetch']) {
    const { controller: c, market, calls } = page()
    market.snapshot.value.installed.unshift({ ...market.snapshot.value.installed[0], managed: false, url: '/readonly', contentDigest: 'readonly' })
    c.selectMore(action, { name: 'hello' })
    assert.equal(c.showUpdate.value, true)
    await c.confirmUpdate()
    assert.equal(calls.length, 1)
    assert.equal(calls[0][0], 'update')
  }
})

test('repairing dependencies requires its own confirmation', async () => {
  const { controller: c, calls, confirmations } = page()
  c.selectMore('repair', { name: 'hello' })
  assert.equal(calls.length, 0)
  assert.equal(confirmations[0].content, 'plugins.repairConfirm')
  await confirmations[0].onPositiveClick()
  assert.equal(calls[0][0], 'repair')
})

for (const action of ['repair', 'uninstall']) {
  test(`${action} closes confirmation without waiting for its background task`, async () => {
    const { controller: c, market, confirmations } = page()
    let finish
    market.manage = () => new Promise(resolve => { finish = resolve })
    c.selectMore(action, { name: 'hello' })
    const confirm = confirmations[0]
    try {
      assert.equal(confirm.onPositiveClick(), undefined)
      assert.equal(c.showLogs.value, false)
      confirm.onAfterLeave()
      assert.equal(c.showLogs.value, true)
    } finally { finish?.(true) }
  })

  test(`failed ${action} is handled by the task instead of keeping confirmation open`, async () => {
    const { controller: c, market, confirmations } = page()
    market.manage = async () => { throw new Error('Dependency operation failed') }
    c.selectMore(action, { name: 'hello' })
    assert.equal(confirmations[0].onPositiveClick(), undefined)
    await Promise.resolve()
    confirmations[0].onAfterLeave()
    assert.equal(c.showLogs.value, true)
  })

  test(`dismissing ${action} confirmation does not open logs or start a task`, () => {
    const { controller: c, confirmations, calls } = page()
    c.selectMore(action, { name: 'hello' })
    confirmations[0].onAfterLeave()
    assert.equal(c.showLogs.value, false)
    assert.equal(calls.length, 0)
  })
}

test('a recognized runtime with an old core cannot start a plugin installation', () => {
  const { controller, market } = page()
  market.snapshot.value.runtime.managementSupported = false
  assert.equal(controller.installDisabled(), true)
  assert.equal(controller.installDisabled('hello'), true)
  market.snapshot.value.runtime.managementSupported = true
  assert.equal(controller.installDisabled(), false)
})
