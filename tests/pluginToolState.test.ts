import assert from 'node:assert/strict'
import { afterEach, beforeEach, test } from 'node:test'
import { updatePluginToolState } from '../src/features/plugin-marketplace/toolState.ts'

const values = new Map<string, string>()

beforeEach(() => {
  values.clear()
  Object.defineProperty(globalThis, 'window', { configurable: true, value: {} })
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
    },
  })
})

afterEach(() => {
  Reflect.deleteProperty(globalThis, 'window')
  Reflect.deleteProperty(globalThis, 'localStorage')
})

test('plugin selection preserves legacy tool settings in the shared store', async () => {
  values.set('pymss-studio:audio-tools', JSON.stringify({ activeTool: 'asr', asrLanguage: 'zh', mergeSortBy: 'name' }))
  await updatePluginToolState({ activePluginTool: 'asr/tool' })
  assert.deepEqual(JSON.parse(values.get('pymss-studio:audio-tools') || 'null'), {
    activeTool: 'asr', activePluginTool: 'asr/tool', asrLanguage: 'zh', mergeSortBy: 'name',
  })
})
