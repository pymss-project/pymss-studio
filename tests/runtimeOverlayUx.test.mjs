import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const appSource = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
const mainSource = readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8')
const manifest = JSON.parse(readFileSync(new URL('../python/runtime-manifest.json', import.meta.url), 'utf8'))

test('an older runtime only prompts for confirmation and model preloading waits for dependency synchronization', () => {
  assert.doesNotMatch(appSource, /app\.updateRuntimeCore\(/)
  assert.doesNotMatch(appSource, /synchronizeRuntimeManifest|runtimeAutoSyncActive/)
  assert.match(appSource, /runtimeCorePromptVisible\.value = true/)
  assert.match(mainSource, /!runtimeCoreSyncAvailable\(activeRuntime, runtime\.manifestVersion\)/)
})

test('runtime overlay declares a stable base generation and v2-compatible core minimums', () => {
  assert.equal(manifest.baseGeneration, 1)
  // Release resolution pins this manifest separately; dependency synchronization
  // must accept newer stable cores while requiring the v2-compatible baseline.
  assert.equal(manifest.common.pymss, 'pymss[proxy]>=2.2.0')
  assert.equal(manifest.common['pymss-core'], 'pymss-core>=0.1.14')
})
