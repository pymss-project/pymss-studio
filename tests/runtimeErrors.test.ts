import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { runtimeErrorDetails, runtimeErrorSummary, runtimeOperationError } from '../src/utils/runtimeErrors.ts'

const locales = Object.fromEntries(['zh-CN', 'en'].map(locale => [locale,
  JSON.parse(readFileSync(new URL(`../src/i18n/${locale}.json`, import.meta.url), 'utf8')).settings,
]))
const translate = (locale: string) => (key: string) => locales[locale][key.replace('settings.', '')]

for (const locale of ['zh-CN', 'en']) {
  test(`${locale} explains an incompatible manifest without repeating the worker's reinstall advice`, () => {
    const failure = runtimeOperationError({
      code: 'RUNTIME_MANIFEST_INCOMPATIBLE',
      message: 'The runtime manifest is newer than this application or has an unknown version. Reinstall the runtime.',
    })
    const summary = runtimeErrorSummary(failure, translate(locale))
    assert.match(summary, locale === 'zh-CN' ? /先更新 Studio/ : /Update Studio first/)
    assert.doesNotMatch(summary, /reinstall|重装/i)
    assert.equal(runtimeErrorDetails(failure), `${failure.code}\n${failure.message}`)
  })

  test(`${locale} translates runtime state and cancellation codes`, () => {
    for (const code of ['RUNTIME_UPDATE_PLAN_INVALID', 'RUNTIME_UPDATE_PLAN_STALE', 'RUNTIME_CORE_UPDATE_INACTIVE', 'RUNTIME_NOT_INSTALLED', 'RUNTIME_BUSY', 'RUNTIME_PERMISSION_DENIED', 'TASK_CANCELLED']) {
      const summary = runtimeErrorSummary({ code, message: 'raw worker error' }, translate(locale))
      assert.equal(typeof summary, 'string', code)
      assert.notEqual(summary, 'raw worker error')
      assert.notEqual(summary, locales[locale].runtimeCoreOperationFailed)
    }
  })
}

test('structured worker diagnostics and unknown errors remain inspectable', () => {
  const failure = runtimeOperationError({
    code: 'RUNTIME_MANIFEST_INCOMPATIBLE', message: 'Manifest version unsupported',
    detail: 'Original traceback', details: { status: 'unknown' },
    extra: { runtimeManifestVersion: 'future' }, appManifestVersion: '2026-10',
  })
  const detail = runtimeErrorDetails(failure)
  for (const expected of ['RUNTIME_MANIFEST_INCOMPATIBLE', 'Original traceback', 'unknown', 'future', '2026-10']) assert.ok(detail.includes(expected))
  assert.equal(runtimeOperationError(failure), failure)
  for (const unknown of [new Error('network unreachable'), 'IPC disconnected', { code: 'FUTURE_RUNTIME_ERROR', message: 'New failure', detail: 'Extra context' }, { code: '__proto__', message: 'Unknown code' }]) {
    assert.equal(runtimeErrorSummary(unknown, translate('zh-CN')), locales['zh-CN'].runtimeCoreOperationFailed)
    assert.ok(runtimeErrorDetails(unknown).includes(typeof unknown === 'string' ? unknown : unknown.message))
  }
})

const source = readFileSync(new URL('../src/stores/task.ts', import.meta.url), 'utf8')
const parsed = ts.createSourceFile('task.ts', source, ts.ScriptTarget.Latest, true)
const declaration = parsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'resolveTaskErrorMessage')
assert.ok(declaration, 'resolveTaskErrorMessage must exist')
const code = ts.transpileModule(declaration.getText(parsed), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

for (const locale of ['zh-CN', 'en']) {
  const messages = JSON.parse(readFileSync(new URL(`../src/i18n/${locale}.json`, import.meta.url), 'utf8'))
  const resolve = vm.runInNewContext(`${code}\nresolveTaskErrorMessage`, {
    runtimeErrorSummary,
    i18n: { global: { t: (key, values = {}) => {
      const [group, name] = key.split('.')
      return Object.entries(values).reduce((text, [name, value]) => text.replaceAll(`{${name}}`, value), messages[group][name])
    } } },
  })

  test(`${locale} explains a required kernel upgrade without discarding the model feature`, () => {
    const message = resolve('RUNTIME_OUTDATED', 'raw constructor error')
    assert.match(message, locale === 'zh-CN' ? /更新内核/ : /update the kernel/i)
    assert.doesNotMatch(message, /raw constructor error/)
  })

  test(`${locale} preserves native crash status and gives a diagnostic recovery path`, () => {
    const message = resolve('WORKER_NATIVE_CRASH', 'Windows access violation (0xC0000005)')
    assert.match(message, /0xC0000005/)
    assert.match(message, locale === 'zh-CN' ? /诊断报告/ : /diagnostics/i)
    assert.match(message, /CPU/)
    assert.doesNotMatch(message, /AMD|ROCm/)
    assert.equal(resolve('UNKNOWN', 'original detail'), 'original detail')
  })

  test(`${locale} task errors explain ROCm failures and preserve the original diagnostic`, () => {
    for (const code of ['ROCM_CONTEXT_LOST', 'ROCM_OUT_OF_MEMORY', 'ROCM_TARGET_MISSING', 'ROCM_RUNTIME_INCOMPATIBLE', 'ROCM_NATIVE_CHECK_FAILED']) {
      const original = 'HIP diagnostic: device 0, operator dispatch failed'
      const message = resolve(code, original)
      assert.match(message, /HIP diagnostic: device 0, operator dispatch failed/)
      assert.notEqual(message, original)
      assert.doesNotMatch(message, /CPU|fallback/i)
    }
    assert.match(resolve('ROCM_CONTEXT_LOST', ''), locale === 'zh-CN' ? /重新创建任务/ : /Start a new task/)
    assert.match(resolve('ROCM_OUT_OF_MEMORY', ''), locale === 'zh-CN' ? /分段长度.*并发/ : /segment size.*concurrent/)
    assert.equal(resolve('OTHER_TASK_ERROR', 'Original error'), 'Original error')
  })
}
