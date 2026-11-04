import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createPinia } from 'pinia'
import { createServer } from 'vite'
import { Window } from 'happy-dom'

const browser = new Window({ url: 'http://localhost' })
globalThis.window = browser
globalThis.document = browser.document
globalThis.localStorage = browser.localStorage
Object.defineProperty(globalThis, 'navigator', { value: browser.navigator, configurable: true })
const vite = await createServer({
  configFile: false, appType: 'custom', optimizeDeps: { noDiscovery: true },
  server: { watch: null, middlewareMode: true, hmr: false },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
})
after(async () => { await vite.close(); await browser.happyDOM.close() })
const { useSettingsStore } = await vite.ssrLoadModule('/src/stores/settings.ts')
const key = 'pymss-studio:app-settings'
const persisted = () => JSON.parse(localStorage.getItem(key))
const saved = () => new Promise(resolve => setTimeout(resolve, 160))

for (const [label, stored, expected] of [
  ['new profile', null, 'auto'],
  ['existing profile', { startupOnboardingSeen: true, themeMode: 'dark' }, 'review'],
  ['explicit auto preference', { pluginDefaultsPolicy: 'auto' }, 'auto'],
  ['explicit review preference', { pluginDefaultsPolicy: 'review' }, 'review'],
]) {
  test(`${label} restores and persists its default tool policy`, async () => {
    localStorage.clear()
    if (stored) localStorage.setItem(key, JSON.stringify(stored))
    const settings = useSettingsStore(createPinia())
    try {
      await settings.initialize()
      assert.equal(settings.pluginDefaultsPolicy, expected)
      await saved()
      assert.equal(persisted().pluginDefaultsPolicy, expected)
      if (stored?.themeMode) assert.equal(persisted().themeMode, stored.themeMode)
    } finally { settings.$dispose() }
  })
}

test('enabling defaults survives a settings store restart', async () => {
  localStorage.setItem(key, JSON.stringify({ startupOnboardingSeen: true }))
  const first = useSettingsStore(createPinia())
  await first.initialize()
  await saved()
  first.pluginDefaultsPolicy = 'auto'
  await saved()
  first.$dispose()
  const next = useSettingsStore(createPinia())
  try {
    await next.initialize()
    assert.equal(next.pluginDefaultsPolicy, 'auto')
  } finally { next.$dispose() }
})

test('plugin source changes are durable before the settings method returns', async () => {
  localStorage.setItem(key, JSON.stringify({ startupOnboardingSeen: true, pluginSource: 'invalid' }))
  const settings = useSettingsStore(createPinia())
  try {
    await settings.initialize()
    assert.equal(settings.pluginSource, 'auto')
    await settings.savePluginSourcePreference('gitcode')
    assert.equal(persisted().pluginSource, 'gitcode')
    const restored = useSettingsStore(createPinia())
    try {
      await restored.initialize()
      assert.equal(restored.pluginSource, 'gitcode')
    } finally { restored.$dispose() }
  } finally { settings.$dispose() }
})

test('plugin view mode defaults to a list and restores a saved card preference', async () => {
  localStorage.setItem(key, JSON.stringify({ startupOnboardingSeen: true, pluginViewMode: 'invalid' }))
  const settings = useSettingsStore(createPinia())
  try {
    await settings.initialize()
    assert.equal(settings.pluginViewMode, 'list')
    settings.pluginViewMode = 'card'
    await saved()
    assert.equal(persisted().pluginViewMode, 'card')
    const restored = useSettingsStore(createPinia())
    try {
      await restored.initialize()
      assert.equal(restored.pluginViewMode, 'card')
    } finally { restored.$dispose() }
  } finally { settings.$dispose() }
})

test('a failed source preference write restores the previous source', async () => {
  localStorage.setItem(key, JSON.stringify({ startupOnboardingSeen: true, pluginSource: 'github' }))
  const settings = useSettingsStore(createPinia())
  try {
    await settings.initialize()
    window.__TAURI_INTERNALS__ = {
      metadata: { currentWindow: { label: 'main' }, currentWebview: { label: 'main' } },
      invoke: async () => { throw new Error('Settings unavailable') },
    }
    await assert.rejects(settings.savePluginSourcePreference('gitcode'), /Settings unavailable/)
    assert.equal(settings.pluginSource, 'github')
  } finally {
    delete window.__TAURI_INTERNALS__
    await saved()
    settings.$dispose()
  }
})
