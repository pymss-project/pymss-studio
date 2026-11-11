import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'
import { segmentIntersectsRect } from '../src/utils/selection.ts'

const descriptor = parse(readFileSync(new URL('../src/views/ResultsView.vue', import.meta.url), 'utf8')).descriptor
const source = ts.createSourceFile('results.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
const names = new Set(['handleResultIconPointerDown', 'handleResultsPointerMove', 'finishResultDragSelection',
  'resetResultClickSuppression', 'handleResultRowClick', 'hasSelectedResultText', 'handleResultsWindowChange', 'handleResultsKeydown'])
const statements = source.statements.filter(statement => ts.isFunctionDeclaration(statement) && names.has(statement.name?.text))
assert.equal(statements.length, names.size)
const code = ts.transpileModule(statements.map(statement => statement.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function environment({ selected = [], selecting = false, bottom = 1000 } = {}) {
  let selection = [...selected], writes = 0, modal = false
  const classes = new Set(), expanded = []
  const rows = Array.from({ length: 10 }, (_, index) => ({
    dataset: { resultId: `row-${index}` },
    getBoundingClientRect: () => ({ left: 20, right: 600, top: 50 + index * 60, bottom: 100 + index * 60 }),
  }))
  const selectedIds = {
    get value() { return selection },
    set value(value) { selection = Array.from(value); writes++ },
  }
  const state = {
    segmentIntersectsRect, resultDragSelection: null, suppressNextResultClick: false,
    selecting: { value: selecting }, selectedResultIds: selectedIds,
    selectedResultSet: { get value() { return new Set(selection) } },
    resultList: { value: {
      querySelectorAll: () => rows,
      closest: () => ({ getBoundingClientRect: () => ({ left: 0, right: 800, top: 40, bottom }) }),
    } },
    document: { querySelector: () => modal, documentElement: { classList: { add: value => classes.add(value), remove: value => classes.delete(value) } } },
    window: { getSelection: () => null, innerWidth: 800, innerHeight: bottom },
    toggleResultSelection(id) { selectedIds.value = selection.includes(id) ? selection.filter(item => item !== id) : [...selection, id] },
    toggleExpanded(id) { expanded.push(id) },
    toggleSelecting() { state.selecting.value = !state.selecting.value; if (!state.selecting.value) selectedIds.value = [] },
  }
  const view = vm.runInNewContext(`${code}\n({${[...names].join(',')}})`, state)
  const event = (extra = {}) => ({ pointerId: 1, pointerType: 'mouse', button: 0, buttons: 1, clientX: 38, clientY: 70,
    preventDefault() {}, ...extra })
  return { state, classes, expanded, selected: () => selection, writes: () => writes,
    modal(value) { modal = value },
    down(id = 'row-0', extra) { view.resetResultClickSuppression(); view.handleResultIconPointerDown(event(extra), id) },
    move(y, x = 300, extra) { view.handleResultsPointerMove(event({ clientX: x, clientY: y, ...extra })) },
    up(extra) { view.finishResultDragSelection(event({ type: 'pointerup', ...extra })) },
    click(id = 'row-0', detail = 1) { view.handleResultRowClick({ detail }, id) },
    blur: view.handleResultsWindowChange,
    escape() { view.handleResultsKeydown({ key: 'Escape', preventDefault() {} }) },
  }
}

test('a click or small mouse jitter keeps normal expansion and selection behavior', () => {
  const env = environment()
  env.down(); env.move(72, 40); env.up(); env.click()
  assert.equal(env.state.selecting.value, false)
  assert.deepEqual(env.selected(), [])
  assert.deepEqual(env.expanded, ['row-0'])
  const selecting = environment({ selecting: true })
  selecting.down(); selecting.up(); selecting.click()
  assert.deepEqual(selecting.selected(), ['row-0'])
})

test('moving 5 pixels activates selection immediately without a hold timer', () => {
  const env = environment()
  env.down(); env.move(75, 38)
  assert.equal(env.state.selecting.value, true)
  assert.deepEqual(env.selected(), ['row-0'])
  assert.ok(env.classes.has('results-drag-selecting'))
})

test('a fast sweep follows whole rows and writes once per event without toggling revisited rows', () => {
  const env = environment({ selecting: true })
  env.down(); env.move(375)
  assert.deepEqual(env.selected(), Array.from({ length: 6 }, (_, i) => `row-${i}`))
  assert.equal(env.writes(), 1)
  env.move(70); env.move(375)
  assert.equal(env.writes(), 1)
  env.up(); env.click('row-5')
  assert.equal(env.selected().length, 6)
  assert.equal(env.classes.size, 0)
})

test('starting on a selected row paints deselection across mixed rows in both directions', () => {
  const env = environment({ selecting: true, selected: ['row-0', 'row-2', 'row-5', 'row-9'] })
  env.down('row-5', { clientX: 38, clientY: 375 }); env.move(70, 200); env.move(375)
  assert.deepEqual(env.selected(), ['row-9'])
  assert.equal(env.writes(), 1)
})

test('the sweep ignores clipped offscreen rows and unrelated pointer IDs', () => {
  const env = environment({ selecting: true, bottom: 400 })
  env.down(); env.move(650, 300, { pointerId: 2 })
  assert.equal(env.writes(), 0)
  env.move(650)
  assert.deepEqual(env.selected(), Array.from({ length: 6 }, (_, i) => `row-${i}`))
  env.up({ pointerId: 2 })
  assert.ok(env.classes.has('results-drag-selecting'))
  env.up(); assert.equal(env.classes.size, 0)
})

test('mouse release, blur, and modal interruption stop gestures and retain keyboard clicks', () => {
  for (const stop of [env => env.blur(), env => env.move(400, 300, { buttons: 0 }), env => { env.modal(true); env.move(400) }]) {
    const env = environment({ selecting: true })
    env.down(); env.move(135); stop(env)
    env.move(375)
    assert.deepEqual(env.selected(), ['row-0', 'row-1'])
    assert.equal(env.classes.size, 0)
    env.click('row-2', 0)
    assert.deepEqual(env.selected(), ['row-0', 'row-1', 'row-2'])
  }
})

test('Escape respects dialogs and a new physical click clears stale click suppression', () => {
  const env = environment({ selecting: true })
  env.down(); env.move(135); env.modal(true); env.escape()
  assert.equal(env.state.selecting.value, true)
  env.modal(false); env.escape()
  assert.equal(env.state.selecting.value, false)
  assert.equal(env.classes.size, 0)
  env.down(); env.up(); env.click()
  assert.deepEqual(env.expanded, ['row-0'])
})

test('touch and secondary mouse buttons never start a selection sweep', () => {
  for (const extra of [{ pointerType: 'touch' }, { button: 2 }]) {
    const env = environment()
    env.down('row-0', extra); env.move(375)
    assert.deepEqual(env.selected(), [])
    assert.equal(env.state.selecting.value, false)
  }
})

test('copying result text does not expand or change the selected rows', () => {
  for (const selecting of [false, true]) {
    const env = environment({ selecting })
    env.state.window.getSelection = () => ({ isCollapsed: false, toString: () => 'song.wav' })
    env.click()
    assert.deepEqual(env.expanded, [])
    assert.deepEqual(env.selected(), [])
  }
})
