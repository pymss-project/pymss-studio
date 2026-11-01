import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../src/stores/task.ts', import.meta.url), 'utf8')
const parsed = ts.createSourceFile('task.ts', source, ts.ScriptTarget.Latest, true)
const declaration = parsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'resolveTaskErrorMessage')
const code = ts.transpileModule(declaration.getText(parsed), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

for (const locale of ['zh-CN', 'en']) {
  const messages = JSON.parse(readFileSync(new URL(`../src/i18n/${locale}.json`, import.meta.url), 'utf8'))
  const resolve = vm.runInNewContext(`${code}\nresolveTaskErrorMessage`, {
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
}
