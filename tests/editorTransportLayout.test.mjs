import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { parse } from 'vue/compiler-sfc'

const path = new URL('../src/components/editor/EditorTransportBar.vue', import.meta.url)
const styles = parse(readFileSync(path, 'utf8')).descriptor.styles.map(item => item.content).join('\n')

test('audio editor wraps intrinsic transport widths within its own container', () => {
  assert.match(styles, /\.editor-transport\s*\{[^}]*flex-wrap:\s*wrap/)
  assert.match(styles, /\.editor-transport__center\s*\{[^}]*grid-template-columns:[^;]*max-content/)
  assert.match(styles, /\.editor-transport__actions\s*\{[^}]*min-width:\s*max-content/)
  assert.doesNotMatch(styles, /@media\s*\(max-width:/)
})

test('offline asset notice spans the complete transport width', () => {
  assert.match(styles, /\.editor-offline-banner\s*\{\s*flex-basis:\s*100%/)
})
