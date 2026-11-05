import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, h, ref } from 'vue'
import { registerWindowCloseGuard, runWindowCloseGuards, runWindowCloseRecoveries, WindowClosePersistenceError } from '../src/utils/windowCloseGuards.ts'

const component = readFileSync(new URL('../src/components/TitleBar.vue', import.meta.url), 'utf8')
const script = component.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const source = ts.createSourceFile('titleBar.ts', script, ts.ScriptTarget.Latest, true)
const code = ts.transpileModule(source.statements.filter(item => !ts.isImportDeclaration(item)).map(item => item.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)) }

function environment({ closeFails = false } = {}) {
  const calls = [], prompts = [], errors = []
  const context = vm.createContext({
    computed, h, ref, NButton: { name: 'Button' }, runWindowCloseGuards, runWindowCloseRecoveries,
    createLogger: () => ({ warn() {} }), useRoute: () => ({ name: 'editor' }),
    useI18n: () => ({ t: key => key }),
    useDialog: () => ({ warning(options) { prompts.push(options); return { destroy() { options.onClose?.() } } } }),
    useMessage: () => ({ error: text => errors.push(text), warning() {}, loading: () => ({ destroy() {} }) }),
    useSettingsStore: () => ({ isModelDirMigrating: false }),
    useTaskStore: () => ({ runningTasks: [] }),
    window: { __TAURI_INTERNALS__: {} }, navigator: { platform: 'Win32' },
    getCurrentWindow: () => ({ label: 'editor',
      async destroy() { calls.push('destroy'); if (closeFails) throw new Error('Destroy failed') },
      async close() { calls.push('close'); if (closeFails) throw new Error('Close failed') } }),
    async invoke(command) { calls.push(command); if (closeFails && command === 'close_current_window') throw new Error('Native close failed') },
    onMounted() {}, onUnmounted() {},
  })
  vm.runInContext(code, context)
  return {
    calls, prompts, errors,
    close: () => vm.runInContext('requestClose()', context),
    choose(text) {
      const action = prompts.at(-1).action().children.find(node => node.children.default() === text)
      assert.ok(action, `Missing close action: ${text}`)
      action.props.onClick()
    },
  }
}

test('save failure presents recovery choices and cancellation leaves the window open without discarding', async t => {
  const env = environment()
  let discarded = false
  const remove = registerWindowCloseGuard(() => { throw new WindowClosePersistenceError('Controller unavailable', async () => { discarded = true }) })
  t.after(remove)
  const closing = env.close()
  await flush()
  assert.equal(env.prompts.length, 1)
  assert.equal(env.prompts[0].action().children.length, 3)
  assert.deepEqual(env.calls, [])
  env.choose('common.continueUsing')
  await closing
  assert.equal(discarded, false)
  assert.deepEqual(env.calls, [])
  assert.deepEqual(env.errors, [])
})

test('retry runs persistence again and only then closes the native window', async t => {
  const env = environment()
  let attempts = 0
  const remove = registerWindowCloseGuard(() => {
    if (++attempts === 1) throw new WindowClosePersistenceError('Capture failed', async () => assert.fail('Unexpected discard'))
  })
  t.after(remove)
  const closing = env.close()
  await flush()
  env.choose('tasks.retrySaveAndClose')
  await closing
  assert.equal(attempts, 2)
  assert.deepEqual(env.calls, ['close_current_window'])
})

test('native close failure resumes suspended work after all guards have passed', async t => {
  const env = environment({ closeFails: true })
  let resumed = 0
  const remove = registerWindowCloseGuard(() => {}, 70, () => { resumed++ })
  t.after(remove)
  await env.close()
  assert.deepEqual(env.calls, ['close_current_window', 'destroy', 'close'])
  assert.equal(resumed, 1)
  assert.deepEqual(env.errors, ['tasks.closeFailed'])
})

test('recovery keyboard actions stop background shortcuts, preserve Space activation and handle Escape', async t => {
  const env = environment()
  const remove = registerWindowCloseGuard(() => { throw new WindowClosePersistenceError('Save failed', async () => {}) })
  t.after(remove)
  const closing = env.close()
  await flush()
  const onKeydown = env.prompts[0].action().props.onKeydown
  for (const key of [' ', 'Enter', 'Delete', 's']) {
    let stopped = false, prevented = false
    onKeydown({ key, stopPropagation() { stopped = true }, preventDefault() { prevented = true } })
    assert.equal(stopped, true)
    assert.equal(prevented, false)
  }
  let escaped = false
  onKeydown({ key: 'Escape', stopPropagation() {}, preventDefault() { escaped = true } })
  await closing
  assert.equal(escaped, true)
  assert.deepEqual(env.calls, [])
  assert.deepEqual(env.errors, [])
})

test('discard waits for plugin release before closing and repeated close clicks share one prompt', async t => {
  const env = environment()
  let release
  const gate = new Promise(resolve => { release = resolve })
  const remove = registerWindowCloseGuard(() => { throw new WindowClosePersistenceError('Save failed', () => gate) })
  t.after(remove)
  const closing = env.close()
  await flush()
  await env.close()
  assert.equal(env.prompts.length, 1)
  env.choose('tasks.discardAndClose')
  await flush()
  assert.deepEqual(env.calls, [])
  release()
  await closing
  assert.deepEqual(env.calls, ['close_current_window'])
})
