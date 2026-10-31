import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
const code = ts.transpileModule(readFileSync(new URL('../../frontend/src/layout.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText

test('tool height follows natural content, coalesces updates and disconnects on disposal', () => {
  const exports = {}, messages = [], scheduled = new Map(), events = new Map()
  let height = 520, next = 0, resized, disconnected = false
  vm.runInNewContext(code, {
    exports,
    window: { parent: { postMessage: value => messages.push(JSON.parse(JSON.stringify(value))) }, addEventListener: (name, callback) => events.set(name, callback), removeEventListener: name => events.delete(name) },
    document: { body: { getBoundingClientRect: () => ({ height }) }, querySelectorAll: () => [] },
    ResizeObserver: class { constructor(callback) { resized = callback } observe() {} disconnect() { disconnected = true } },
    MutationObserver: class { observe() {} disconnect() {} },
    requestAnimationFrame: callback => { scheduled.set(++next, callback); return next }, cancelAnimationFrame: id => scheduled.delete(id),
  })
  const flush = () => { const callbacks = [...scheduled.values()]; scheduled.clear(); callbacks.forEach(callback => callback()) }
  const dispose = exports.observeToolLayout()
  resized(); resized(); flush()
  assert.deepEqual(messages, [{ jsonrpc: '2.0', method: 'pymss.resize', params: { height: 520 } }])
  resized(); flush(); assert.equal(messages.length, 1)
  height = 360; resized(); flush(); assert.equal(messages.at(-1).params.height, 360)
  height = 900; resized(); dispose(); flush()
  assert.equal(messages.length, 2)
  assert.equal(disconnected, true)
  assert.equal(events.size, 0)
})

test('clipped floating controls reserve stable space and release it on dismissal', () => {
  const exports = {}, messages = [], scheduled = new Map()
  const observed = new Set()
  let next = 0, mutated, resized, disconnected = false
  let popups = [], height = 273, bounds = { top: -21, bottom: 123, height: 144 }
  const popup = { getClientRects: () => [bounds], getBoundingClientRect: () => bounds }
  const window = { innerHeight: 273, parent: { postMessage: message => messages.push(message) }, addEventListener() {}, removeEventListener() {} }
  vm.runInNewContext(code, {
    exports, window,
    document: { body: { getBoundingClientRect: () => ({ height }) }, querySelectorAll: () => popups },
    getComputedStyle: () => ({ visibility: 'visible' }),
    ResizeObserver: class { constructor(callback) { resized = callback } observe(element) { observed.add(element) } unobserve(element) { observed.delete(element) } disconnect() { observed.clear() } },
    MutationObserver: class { constructor(callback) { mutated = callback } observe() {} disconnect() { disconnected = true } },
    requestAnimationFrame: callback => { scheduled.set(++next, callback); return next }, cancelAnimationFrame: id => scheduled.delete(id),
  })
  const flush = () => { const callbacks = [...scheduled.values()]; scheduled.clear(); callbacks.forEach(callback => callback()) }
  const dispose = exports.observeToolLayout()
  flush()
  popups = [popup]; mutated(); flush()
  assert.equal(messages.at(-1).params.height, 433)
  assert.equal(observed.has(popup), true)
  // The menu now fits; reservation stays until it closes to avoid resize feedback.
  window.innerHeight = 433; bounds = { top: 165, bottom: 309, height: 144 }
  resized(); flush()
  assert.equal(messages.length, 2)
  // Naive UI can keep the follower wrapper mounted after hiding its menu.
  bounds = { top: 165, bottom: 165, height: 0 }; mutated(); flush()
  assert.equal(messages.at(-1).params.height, 273)
  assert.equal(observed.has(popup), false)
  // A menu that already fits must not add whitespace.
  height = 700; window.innerHeight = 700; bounds = { top: 165, bottom: 309, height: 144 }; popups = [popup]; mutated(); flush()
  assert.equal(messages.at(-1).params.height, 700)
  mutated(); dispose(); flush()
  assert.equal(disconnected, true)
  assert.equal(observed.size, 0)
})

test('overlay close resends a height that the host ignored while the overlay was open', () => {
  const exports = {}, messages = [], scheduled = new Map(), events = new Map()
  let height = 500, next = 0, resized, overlay = false, hostHeight = 0
  const window = {
    parent: { postMessage: message => { messages.push(message); if (!overlay) hostHeight = message.params.height } },
    addEventListener: (name, callback) => events.set(name, callback),
    removeEventListener: name => events.delete(name),
    dispatchEvent: event => events.get(event.type)?.(),
  }
  vm.runInNewContext(code, {
    exports, window, Event,
    document: { body: { getBoundingClientRect: () => ({ height }) }, querySelectorAll: () => [] },
    ResizeObserver: class { constructor(callback) { resized = callback } observe() {} disconnect() {} },
    MutationObserver: class { observe() {} disconnect() {} },
    requestAnimationFrame: callback => { scheduled.set(++next, callback); return next }, cancelAnimationFrame: id => scheduled.delete(id),
  })
  const flush = () => { const callbacks = [...scheduled.values()]; scheduled.clear(); callbacks.forEach(callback => callback()) }
  const dispose = exports.observeToolLayout()
  flush()
  overlay = true; height = 800; resized(); flush()
  assert.equal(hostHeight, 500)
  overlay = false; exports.refreshToolLayout(); resized(); flush()
  assert.equal(hostHeight, 800)
  assert.equal(messages.length, 3)
  resized(); flush(); assert.equal(messages.length, 3)
  dispose(); exports.refreshToolLayout(); flush()
  assert.equal(events.size, 0)
  assert.equal(messages.length, 3)
})

test('the standalone example reports height after closing its overlay and removes the listener on pagehide', () => {
  const scheduled = new Map(), events = new Map(), messages = []
  let height = 500, hostHeight = 0, overlayOpen = false, next = 0, resized, disconnected = false
  const window = {
    parent: { postMessage: message => {
      messages.push(message)
      if (message.method === 'pymss.overlay') overlayOpen = message.params.open
      if (message.method === 'pymss.resize' && !overlayOpen) hostHeight = message.params.height
    } },
    addEventListener: (name, callback) => events.set(name, callback),
    removeEventListener: name => events.delete(name),
    dispatchEvent: event => events.get(event.type)?.(),
  }
  const studio = {}
  const app = { use() { return this }, mount() {}, unmount: () => studio.overlay(false) }
  const context = {
    window, Event, navigator: { language: 'en' },
    document: { getElementById: () => ({ getBoundingClientRect: () => ({ height }) }) },
    ResizeObserver: class { constructor(callback) { resized = callback } observe() {} disconnect() { disconnected = true } },
    requestAnimationFrame: callback => { scheduled.set(++next, callback); return next }, cancelAnimationFrame: id => scheduled.delete(id),
    require: name => ({ vue: { reactive: value => value, createApp: () => app }, './i18n': { resolveLocale: value => value, messages: {} }, 'vue-i18n': { createI18n: () => ({}) }, './studio': studio })[name] || {},
  }
  const compile = path => ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  vm.runInNewContext(compile('../../../examples/audio_gain/ui/src/studio.ts'), { ...context, exports: studio })
  vm.runInNewContext(compile('../../../examples/audio_gain/ui/src/main.ts'), { ...context, exports: {} })
  const flush = () => { const callbacks = [...scheduled.values()]; scheduled.clear(); callbacks.forEach(callback => callback()) }
  flush()
  studio.overlay(true); height = 800; resized(); flush()
  assert.equal(hostHeight, 500)
  studio.overlay(false); flush()
  assert.equal(hostHeight, 800)
  assert.deepEqual(messages.slice(-2).map(message => message.method), ['pymss.overlay', 'pymss.resize'])
  events.get('pagehide')()
  assert.equal(disconnected, true)
  assert.equal(events.has(studio.LAYOUT_REFRESH_EVENT), false)
  assert.equal(scheduled.size, 0)
})
