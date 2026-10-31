import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'

function componentComputed(name, scope) {
  const source = parse(readFileSync(new URL('../../../plugins/asr/ui/src/AsrTool.vue', import.meta.url), 'utf8')).descriptor.scriptSetup.content
  const parsed = ts.createSourceFile('asr.ts', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const declaration = parsed.statements.filter(ts.isVariableStatement)
    .flatMap(node => [...node.declarationList.declarations])
    .find(node => node.name.getText(parsed) === name)
  assert.ok(declaration?.initializer, `${name} must exist`)
  const code = ts.transpileModule(`const ${name} = ${declaration.initializer.getText(parsed)}`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  return vm.runInNewContext(`${code}\n${name}`, { computed: fn => fn(), ...scope })
}

test('the ASR page only offers removal for removable components', () => {
  for (const [status, expected] of [
    [null, false],
    [{ installed: false, present: false, canUninstall: false }, false],
    [{ installed: true, present: true, source: 'environment', canUninstall: false }, false],
    [{ installed: true, present: true, source: 'environment', canUninstall: true }, true],
    [{ installed: true, present: true, source: 'managed', canUninstall: true }, true],
    [{ installed: true }, true],
  ]) {
    assert.equal(componentComputed('funAsrCanUninstall', { funAsrStatus: { value: status } }), expected)
  }
})
