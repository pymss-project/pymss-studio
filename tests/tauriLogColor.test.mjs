import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

// Execute the wrapper setup with its native process boundary stubbed. The final
// CLI import is the only executable statement excluded from this harness.
const scriptUrl = new URL('../scripts/tauri.mjs', import.meta.url)
const source = ts.createSourceFile('tauri.mjs', readFileSync(scriptUrl, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.JS)
const setup = source.statements.filter(node => !ts.isImportDeclaration(node)
  && !(ts.isExpressionStatement(node) && ts.isAwaitExpression(node.expression)
    && ts.isCallExpression(node.expression.expression)
    && node.expression.expression.expression.kind === ts.SyntaxKind.ImportKeyword))
  .map(node => node.getText(source)).join('\n')
const code = ts.transpileModule(setup, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  transformers: { before: [context => root => {
    const visit = node => ts.isMetaProperty(node) && node.keywordToken === ts.SyntaxKind.ImportKeyword
      ? ts.factory.createIdentifier('moduleMeta') : ts.visitEachChild(node, visit, context)
    return ts.visitNode(root, visit)
  }] },
}).outputText

function environment({ args = ['dev'], tty = true, env = {} } = {}) {
  const process = {
    argv: ['node', 'scripts/tauri.mjs', ...args], stderr: { isTTY: tty }, env: { ...env },
    exit: status => { throw new Error(`Unexpected wrapper exit: ${status}`) },
  }
  vm.runInNewContext(code, {
    process, fileURLToPath, path, moduleMeta: { url: scriptUrl.href }, exports: {},
    spawnSync: () => ({ status: 0 }),
  })
  return process.env
}

test('interactive dev forwards color support through Tauri and Cargo', () => {
  assert.equal(environment().PYMSS_STUDIO_TERMINAL_COLOR, '1')
  assert.equal(environment().FORCE_COLOR, undefined)
  assert.equal(environment({ args: ['-v', 'dev', '--config', 'preview.json'] }).PYMSS_STUDIO_TERMINAL_COLOR, '1')
})

test('redirected dev output and non-dev commands do not force ANSI', () => {
  assert.equal(environment({ tty: false }).PYMSS_STUDIO_TERMINAL_COLOR, undefined)
  for (const args of [[], ['build'], ['--help'], ['info']]) {
    assert.equal(environment({ args }).PYMSS_STUDIO_TERMINAL_COLOR, undefined)
  }
})

test('explicit color choices and NO_COLOR are preserved', () => {
  for (const value of ['', '0', '1', '2', '3']) {
    assert.deepEqual(environment({ env: { FORCE_COLOR: value } }), { FORCE_COLOR: value })
  }
  for (const value of ['', '1']) {
    assert.deepEqual(environment({ env: { NO_COLOR: value } }), { NO_COLOR: value })
  }
  assert.deepEqual(environment({ env: { NO_COLOR: '1', FORCE_COLOR: '1' } }), { NO_COLOR: '1', FORCE_COLOR: '1' })
  for (const value of ['0', '1']) {
    assert.deepEqual(environment({ env: { PYMSS_STUDIO_TERMINAL_COLOR: value } }), { PYMSS_STUDIO_TERMINAL_COLOR: value })
  }
})
