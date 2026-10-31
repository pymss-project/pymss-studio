import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { parse } from 'vue/compiler-sfc'

const source = parse(readFileSync(new URL('../../../plugins/slicer/ui/src/SlicerTool.vue', import.meta.url), 'utf8')).descriptor.scriptSetup.content
const code = ts.transpileModule(`${source}\nexports.valid = valid; exports.run = run;`, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText

function slicer() {
  const values = new Map(), calls = [], exports = {}
  vm.runInNewContext(code, {
    exports,
    require: name => ({
      '@/sdk': { toolRef: (key, value) => { const ref = { value }; values.set(key, ref); return ref } },
      vue: { computed: getter => ({ get value() { return getter() } }), watch() {} },
      'vue-i18n': { useI18n: () => ({ t: key => key }) },
      'naive-ui': { useMessage: () => ({ error() {} }) },
      '@/stores/settings': { useSettingsStore: () => ({ outputDir: '/output' }) },
      '@/features/audio-tools/runtime': { useAudioToolRuntime: () => ({ stateFor: () => ({}), execute: (...args) => calls.push(args) }) },
    })[name] || {},
  })
  return { ...exports, values, calls }
}

test('slicer refuses cleared and non-finite numeric controls before starting an operation', () => {
  const { valid, run, values, calls } = slicer()
  assert.equal(valid.value, true)
  for (const name of ['threshold', 'minLength', 'minInterval', 'hopSize', 'maxSilKept']) {
    const ref = values.get(name), original = ref.value
    for (const value of [null, undefined, NaN, Infinity, -Infinity, false, '10']) {
      ref.value = value
      assert.equal(valid.value, false, `${name}=${String(value)}`)
      run()
      assert.equal(calls.length, 0)
    }
    ref.value = original
  }
  values.get('threshold').value = -40.5
  assert.equal(valid.value, true)
  run()
  assert.equal(calls.length, 1)
  assert.equal(calls[0][1].threshold, -40.5)
})

test('slicer requires integer timings and preserves the ordering constraints', () => {
  const { valid, values } = slicer()
  for (const name of ['minLength', 'minInterval', 'hopSize', 'maxSilKept']) {
    const ref = values.get(name), original = ref.value
    ref.value += 0.5
    assert.equal(valid.value, false, name)
    ref.value = original
  }
  values.get('hopSize').value = 301
  assert.equal(valid.value, false)
  values.get('hopSize').value = 10
  values.get('threshold').value = 0
  assert.equal(valid.value, true)
})
