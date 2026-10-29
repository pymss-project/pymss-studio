import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const srcDir = path.join(root, 'src')
const i18nDir = path.join(srcDir, 'i18n')

/**
 * Keys built at runtime instead of written out in full. The scans below cannot see these,
 * so they are exempt from the unused check. Add a prefix here when a new dynamic `t()` call
 * site appears — otherwise its keys look dead and get deleted.
 *
 *   t(`settings.themeAccent${...}`)  — SettingsView.vue, StartupOnboarding.vue
 *   t(`nav.${route.name}`)           — TitleBar.vue
 *   advanced-editor node metadata    — litegraph/nodeLocalization.ts
 */
const DYNAMIC_KEY_PREFIXES = [
  'settings.themeAccent',
  'nav.',
  'workflows.advancedEditor.categories.',
  'workflows.advancedEditor.nodes.',
  'workflows.advancedEditor.fields.',
  'workflows.advancedEditor.menu.',
  // PluginTaskStatus / PluginLogDialog use typed action, phase and outcome IDs.
  'plugins.actions.',
  'plugins.phases.',
  'plugins.taskStates.',
]

/**
 * Copy for features that exist in code but are not surfaced yet. Unreferenced on purpose —
 * listing them here is what separates "kept deliberately" from "forgotten".
 *
 *   projects.* — createBlankProject() in stores/editor.ts is kept for the import-first
 *                editor flow; its entry point stays hidden until external audio import
 *                has a proper UI.
 *
 * Anything not listed here and not referenced is dead and should be deleted.
 */
const DORMANT_KEYS = [
  'projects.blankProject', 'projects.createBlank', 'projects.createSuccess',
]

function collectSourceFiles(dir: string): string[] {
  const files: string[] = []
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry)
    if (full.startsWith(i18nDir)) continue
    if (statSync(full).isDirectory()) files.push(...collectSourceFiles(full))
    else if (full.endsWith('.vue') || full.endsWith('.ts')) files.push(full)
  }
  return files
}

function flatten(value: Record<string, unknown>, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [key, child] of Object.entries(value)) {
    if (child && typeof child === 'object') Object.assign(out, flatten(child as Record<string, unknown>, `${prefix}${key}.`))
    else out[`${prefix}${key}`] = String(child)
  }
  return out
}

const zh = flatten(JSON.parse(readFileSync(path.join(i18nDir, 'zh-CN.json'), 'utf-8')))
const en = flatten(JSON.parse(readFileSync(path.join(i18nDir, 'en.json'), 'utf-8')))
const source = collectSourceFiles(srcDir).map((file) => readFileSync(file, 'utf-8')).join('\n')

// Keys passed straight to t('a.b') / $t("a.b"). These must resolve, or the UI renders the
// raw path.
const calledDirectly = new Set<string>()
for (const match of source.matchAll(/\$?t\(\s*['"]([a-zA-Z][\w]*(?:\.[\w]+)+)['"]/g)) {
  calledDirectly.add(match[1])
}

// Any quoted occurrence of a key counts as a use: several keys reach t() through lookup
// tables (`{ advanced_parameters: 'workflows.simpleReasonAdvancedParameters' }`) rather than a direct
// call. Requiring the quotes is what keeps property access like `editor.undo` from matching.
const quoted = new Set<string>()
for (const match of source.matchAll(/['"`]([a-zA-Z][\w]*(?:\.[\w]+)+)['"`]/g)) {
  quoted.add(match[1])
}

function isDynamic(key: string) {
  return DYNAMIC_KEY_PREFIXES.some((prefix) => key.startsWith(prefix))
}

test('both locales define exactly the same keys', () => {
  const onlyZh = Object.keys(zh).filter((key) => !(key in en))
  const onlyEn = Object.keys(en).filter((key) => !(key in zh))
  assert.deepEqual(onlyZh, [], `keys missing from en.json: ${onlyZh.join(', ')}`)
  assert.deepEqual(onlyEn, [], `keys missing from zh-CN.json: ${onlyEn.join(', ')}`)
})

test('every key referenced in source exists in both locales', () => {
  // A missing key renders as the raw path in the UI, which no type check would catch.
  assert.notEqual(calledDirectly.size, 0, 'the reference scan found nothing — the regex is broken')
  const missing = [...calledDirectly].filter((key) => !(key in zh) || !(key in en))
  assert.deepEqual(missing, [], `referenced but undefined: ${missing.join(', ')}`)
})

test('no locale key is left unreferenced', () => {
  const orphans = Object.keys(zh)
    .filter((key) => !quoted.has(key) && !isDynamic(key) && !DORMANT_KEYS.includes(key))
  assert.deepEqual(orphans, [], `unused keys — delete them, or add to DORMANT_KEYS with a reason: ${orphans.join(', ')}`)
})

test('every dormant key still exists', () => {
  // Guards the allowlist itself: once a dormant key is deleted or wired up, its entry here
  // is stale and must be removed rather than silently masking a future orphan.
  const missing = DORMANT_KEYS.filter((key) => !(key in zh))
  assert.deepEqual(missing, [], `DORMANT_KEYS lists keys that no longer exist: ${missing.join(', ')}`)
  const nowUsed = DORMANT_KEYS.filter((key) => quoted.has(key))
  assert.deepEqual(nowUsed, [], `these are referenced now — drop them from DORMANT_KEYS: ${nowUsed.join(', ')}`)
})

test('no locale value is an empty string', () => {
  const blanks = Object.entries({ ...zh, ...en })
    .filter(([, value]) => !value.trim())
    .map(([key]) => key)
  assert.deepEqual(blanks, [], `empty translations: ${blanks.join(', ')}`)
})
