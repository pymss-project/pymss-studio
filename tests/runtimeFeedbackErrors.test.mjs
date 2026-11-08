import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { runtimeErrorSummary } from '../src/utils/runtimeErrors.ts'

const source = readFileSync(new URL('../src/stores/task.ts', import.meta.url), 'utf8')
const parsed = ts.createSourceFile('task.ts', source, ts.ScriptTarget.Latest, true)
const declaration = parsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'resolveTaskErrorMessage')
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
