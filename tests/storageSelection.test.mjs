import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'
import { edgeScrollVelocity, segmentIntersectsRect } from '../src/utils/selection.ts'

const source = ts.createSourceFile('models.ts', parse(readFileSync(new URL('../src/views/ModelsView.vue', import.meta.url), 'utf8')).descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const names = new Set(['canSelectStorageItem', 'setStorageSelected', 'selectStorageResults', 'clearStorageSelection', 'consumeStorageClick',
  'startStorageDrag', 'paintStorageSelection', 'scrollStorageDrag', 'handleStoragePointerMove', 'handleStorageScroll', 'finishStorageDrag'])
const functions = source.statements.filter(statement => ts.isFunctionDeclaration(statement) && names.has(statement.name?.text))
assert.equal(functions.length, names.size)
const code = ts.transpileModule(functions.map(statement => statement.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function environment(selected = []) {
  const items = Array.from({ length: 12 }, (_, i) => ({ selectionKey: `catalog:${i}`, id: String(i), kind: 'catalog', sizeBytes: i === 2 ? 0 : 100 }))
  let selection = selected, scroll = 0, frameId = 0, writes = 0
  const frames = new Map(), classes = new Set()
  const list = {
    get scrollTop() { return scroll }, set scrollTop(value) { scroll = Math.max(0, Math.min(540, value)) },
    getBoundingClientRect: () => ({ left: 0, right: 600, top: 100, bottom: 400 }),
    querySelectorAll: () => items.map((item, i) => ({ dataset: { storageKey: item.selectionKey },
      getBoundingClientRect: () => ({ left: 10, right: 590, top: 100 + i * 70 - scroll, bottom: 160 + i * 70 - scroll }),
    })),
  }
  const state = { segmentIntersectsRect, edgeScrollVelocity, storageDrag: null, storageSuppressClick: false,
    storageScrollFrame: null, storageScrollTime: 0, storageList: { value: list }, storageBusy: { value: false },
    selectedStorageModels: { get value() { return selection }, set value(value) { selection = Array.from(value); writes++ } },
    storageSelection: { get value() { return new Set(selection) } }, storageModels: { value: items },
    allStorageModelsByKey: { value: new Map(items.map(item => [item.selectionKey, item])) },
    isDeletingModel: id => id === '10',
    window: { getSelection: () => null },
    document: { querySelector: () => null, documentElement: { classList: { add: value => classes.add(value), remove: value => classes.delete(value) } } },
    requestAnimationFrame(fn) { const id = ++frameId; frames.set(id, fn); return id }, cancelAnimationFrame: id => frames.delete(id),
  }
  const view = vm.runInNewContext(`${code}\n({${[...names].join(',')}})`, state)
  const event = extra => ({ pointerId: 1, pointerType: 'mouse', button: 0, buttons: 1, clientX: 30, clientY: 130, preventDefault() {}, ...extra })
  return { state, frames, list, classes, selected: () => selection, writes: () => writes,
    down(index = 0) { view.startStorageDrag(event({ clientY: 130 + index * 70 - scroll }), items[index]) },
    move(y, extra) { view.handleStoragePointerMove(event({ clientX: 200, clientY: y, ...extra })) },
    up(extra) { view.finishStorageDrag(event({ type: 'pointerup', ...extra })) },
    tick(time) { const [id, callback] = frames.entries().next().value; frames.delete(id); callback(time) },
    view,
  }
}

test('filtered select-all preserves hidden choices and excludes missing files and active deletions', () => {
  const h = environment(['catalog:11'])
  h.state.storageModels.value = h.state.storageModels.value.slice(0, 5)
  h.view.selectStorageResults(true)
  assert.deepEqual(h.selected(), ['catalog:11', 'catalog:0', 'catalog:1', 'catalog:3', 'catalog:4'])
  h.view.selectStorageResults(false)
  assert.deepEqual(h.selected(), ['catalog:11'])
  h.view.setStorageSelected('catalog:10', true); assert.deepEqual(h.selected(), ['catalog:11'])
  h.view.clearStorageSelection(); assert.deepEqual(h.selected(), [])
})

test('a fast path sweeps whole visible rows once and can paint deselection back', () => {
  const h = environment()
  h.down(); h.move(335)
  assert.deepEqual(h.selected(), ['catalog:0', 'catalog:1', 'catalog:3'])
  h.move(130); h.move(335); assert.equal(h.writes(), 1)
  h.up(); assert.equal(h.classes.size, 0)
  h.down(); h.move(335); h.up(); assert.deepEqual(h.selected(), [])
})

test('edge scrolling selects newly reached rows without another mouse movement', () => {
  const h = environment()
  h.down(); h.move(395)
  for (let i = 1; i <= 28; i++) h.tick(i * 16)
  assert.ok(h.list.scrollTop > 150)
  assert.ok(h.selected().includes('catalog:7'))
  assert.ok(!h.selected().includes('catalog:2'))
  assert.ok(!h.selected().includes('catalog:10'))
  h.up(); assert.equal(h.frames.size, 0); assert.equal(h.classes.size, 0)
})

test('manual scrolling accounts for rows crossed under a stationary drag', () => {
  const h = environment()
  h.down(); h.move(395); h.list.scrollTop = 210; h.view.handleStorageScroll()
  assert.ok(h.selected().includes('catalog:7'))
  h.up()
})

test('refresh and deletion freeze selection and stop an active gesture', () => {
  const h = environment(['catalog:0'])
  h.down(); h.move(200); h.state.storageBusy.value = true; h.move(395)
  const before = h.selected()
  h.view.clearStorageSelection(); h.view.selectStorageResults(true); h.view.setStorageSelected('catalog:5', true)
  assert.deepEqual(h.selected(), before)
  assert.equal(h.classes.size, 0); assert.equal(h.frames.size, 0)
})

test('a post-drag mouse click is consumed while keyboard clicks and ordinary taps remain usable', () => {
  const h = environment()
  h.down(); h.move(335); h.up()
  let consumed = 0
  const event = detail => ({ detail, preventDefault() { consumed++ }, stopPropagation() {} })
  h.view.consumeStorageClick(event(1)); assert.equal(consumed, 1)
  h.state.storageSuppressClick = true; h.view.consumeStorageClick(event(0)); assert.equal(consumed, 1)
  h.down(); h.up(); h.view.consumeStorageClick(event(1)); assert.equal(consumed, 1)
})
