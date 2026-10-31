import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import * as Vue from 'vue'
import { parse, compileTemplate } from 'vue/compiler-sfc'
import { renderToString } from 'vue/server-renderer'
import ts from 'typescript'

test('the compiled operation template passes report through the named result slot', async () => {
  const source = readFileSync(new URL('../../frontend/src/components/OperationTool.vue', import.meta.url), 'utf8')
  const { descriptor } = parse(source)
  const compiled = compileTemplate({ source: descriptor.template.content, filename: 'OperationTool.vue', id: 'operation-slot' })
  assert.deepEqual(compiled.errors, [])
  const code = ts.transpileModule(compiled.code, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
    .replace(/import \{([^}]+)\} from "vue"/g, (_, imports) => `const {${imports.replace(/\bas\b/g, ':')}} = Vue`)
    .replace('export function render', 'return function render')
  const render = new Function('Vue', code)(Vue)
  const output = { kind: 'object', value: { lufs: -20 } }
  const OperationTool = {
    setup: () => ({
      config: { titleKey: 'title', operations: [] }, t: key => key,
      results: { report: output }, running: false, progress: null, stateLabel: 'ready', elapsedLabel: '00:00',
      activity: [], audioInputs: [], fields: [], loading: false, canRun: true, error: '', cancelled: false, spec: {},
      outputText: value => JSON.stringify(value),
    }),
    render,
  }
  const app = Vue.createSSRApp({
    render: () => Vue.h(OperationTool, null, {
      result: ({ port, output }) => Vue.h('strong', { 'data-rendered-port': port }, output.value.lufs),
    }),
  })
  app.config.warnHandler = () => {}
  for (const name of ['ToolPanelShell', 'ToolActivityDialog', 'AudioToolHeader', 'n-button']) {
    app.component(name, { setup: (_, { slots }) => () => Vue.h('div', null, Object.values(slots).flatMap(slot => slot())) })
  }
  const html = await renderToString(app)
  assert.match(html, /data-rendered-port="report">-20<\/strong>/)
  assert.doesNotMatch(html, /tool-result-json/)
})
