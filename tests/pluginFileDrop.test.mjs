import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'

const source = ts.createSourceFile('host.ts', parse(readFileSync(new URL('../src/features/plugin-marketplace/PluginViewHost.vue', import.meta.url), 'utf8')).descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const names = new Set(['clearFileDropHover', 'onNativeFileDrop'])
const code = ts.transpileModule(source.statements.filter(statement => ts.isFunctionDeclaration(statement) && names.has(statement.name?.text)).map(statement => statement.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function host() {
  const messages = []
  const frame = { getBoundingClientRect: () => ({ left: 100, top: 80 }) }
  const state = { frame: { value: frame }, frameLoaded: { value: true }, status: { value: 'ready' },
    disposed: false, dropHovered: false, allowed: () => true,
    window: { devicePixelRatio: 2 }, document: { elementFromPoint: () => frame },
    postToFrame: value => messages.push(JSON.parse(JSON.stringify(value))),
  }
  const view = vm.runInNewContext(`${code}\n({onNativeFileDrop})`, state)
  return { messages, state, send(payload) { view.onNativeFileDrop({ position: { x: 240, y: 200 }, ...payload }) } }
}

test('native drop events reach the visible plugin in iframe CSS coordinates', () => {
  const h = host()
  h.send({ type: 'enter', paths: ['C:/Audio/song.wav'] })
  h.send({ type: 'over' })
  h.send({ type: 'drop', paths: ['C:/Audio/song.wav'] })
  assert.deepEqual(h.messages.at(-1), { jsonrpc: '2.0', method: 'host.filesDrop', params: {
    type: 'drop', position: { x: 20, y: 20 }, paths: ['C:/Audio/song.wav'],
  } })
  assert.equal(h.state.dropHovered, false)
})

test('navigation, other dialogs, missing permission and hidden or disposed frames never receive paths', () => {
  for (const block of [h => { h.state.document.elementFromPoint = () => ({}) },
    h => { h.state.allowed = () => false }, h => { h.state.frameLoaded.value = false },
    h => { h.state.status.value = 'loading' }, h => { h.state.disposed = true }]) {
    const h = host(); block(h); h.send({ type: 'drop', paths: ['private.wav'] })
    assert.equal(h.messages.length, 0)
  }
})

test('leaving the iframe clears its hover once, including invalid native coordinates', () => {
  for (const event of [{ type: 'leave' }, { type: 'over', position: { x: NaN, y: 200 } }]) {
    const h = host(); h.send({ type: 'enter', paths: ['song.wav'] }); h.send(event); h.send(event)
    assert.deepEqual(h.messages.at(-1).params, { type: 'leave' })
    assert.equal(h.messages.length, 2)
  }
})

test('public SDK drop subscriptions accept only parent messages and can be removed', () => {
  const handlers = {}, parent = { postMessage() {} }, events = []
  const window = { parent, addEventListener: (name, handler) => { handlers[name] = handler } }
  vm.runInNewContext(readFileSync(new URL('../src-tauri/resources/plugin-sdk/pymss-sdk.js', import.meta.url), 'utf8'), { window })
  const remove = window.pymss.onFileDrop(event => events.push(event))
  const data = { jsonrpc: '2.0', method: 'host.filesDrop', params: { type: 'drop', paths: ['song.wav'] } }
  handlers.message({ source: {}, data }); assert.equal(events.length, 0)
  handlers.message({ source: parent, data }); assert.equal(events.length, 1)
  remove(); handlers.message({ source: parent, data }); assert.equal(events.length, 1)
})
