import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'
import { computed, ref } from 'vue'

const descriptor = parse(readFileSync(new URL('../src/views/PluginsView.vue', import.meta.url), 'utf8')).descriptor
const source = ts.createSourceFile('plugins.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const code = ts.transpileModule(source.statements.filter(node => !ts.isImportDeclaration(node)).map(node => node.getText(source)).join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function page() {
  const calls = [], warnings = [], confirmations = []
  const market = {
    snapshot: ref({ runtime: { identity: 'cpu' }, catalog: [], installed: [{ name: 'hello', version: '1.0' }] }),
    updates: ref({}), loading: ref(false), catalogLoading: ref(false), error: ref(''), busy: ref(false), logs: ref([]), activity: ref(null),
    openMarketplace() {}, refresh() {}, cancel() {}, manage: async (...args) => { calls.push(args) },
  }
  const scope = { computed, ref, nextTick: async callback => callback?.(), watch() {}, onMounted() {},
    useI18n: () => ({ t: key => key, locale: ref('zh-CN') }), usePluginMarketplace: () => market,
    useRouter: () => ({ push() {} }), useAppStore: () => ({}),
    useDialog: () => ({ warning: options => confirmations.push(options) }),
    useMessage: () => ({ success() {}, warning: value => warnings.push(value), error() {} }),
  }
  const controller = vm.runInNewContext(`${code}\n({ selectMore, confirmUpdate, showUpdate, installDependencies, updateRuntime, showLogs })`, scope)
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

test('repairing dependencies requires its own confirmation', async () => {
  const { controller: c, calls, confirmations } = page()
  c.selectMore('repair', { name: 'hello' })
  assert.equal(calls.length, 0)
  assert.equal(confirmations[0].content, 'plugins.repairConfirm')
  await confirmations[0].onPositiveClick()
  assert.equal(calls[0][0], 'repair')
})
