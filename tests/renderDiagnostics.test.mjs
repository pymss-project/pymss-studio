import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'
import { effectScope, reactive, nextTick, watch } from 'vue'

const transpile = (path, prefix = '') => ts.transpileModule(prefix + readFileSync(new URL(path, import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText

test('render observations read actual classes and bounded visibility without creating graphics contexts', () => {
  const classes = new Set(['dark-theme', 'light-theme'])
  const exports = {}
  let inspected = 0
  const page = { getBoundingClientRect: () => { inspected++; return { width: 100, height: 10 } } }
  const document = { documentElement: { classList: { contains: cls => classes.has(cls) }, lang: 'en' }, visibilityState: 'visible',
    querySelector: () => ({ classList: { contains: () => true } }),
    querySelectorAll: selector => selector === '.page' ? Array(40).fill(page) : [],
    createElement: () => { throw new Error('Diagnostics must not create a canvas') },
  }
  vm.runInNewContext(transpile('../src/utils/renderDiagnostics.ts'), {
    exports, document, navigator: { userAgent: 'WebKit', platform: 'MacIntel', language: 'en' },
    window: { devicePixelRatio: 2, innerWidth: 1280, innerHeight: 800, matchMedia: () => ({ matches: false }) },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', getPropertyValue: () => ' rgb(1, 2, 3) ' }),
  })
  const result = exports.captureRenderDiagnostics({ theme: 'system', resolvedTheme: 'dark', animations: false, scale: 1.2, locale: 'system', resolvedLocale: 'en', routePath: '/plugin-workspace?secret=hidden#token' })
  assert.equal(result.themeClass, 'mixed')
  assert.equal(result.animationsDisabled, true)
  assert.equal(result.routePath, '/plugin-workspace')
  assert.equal(result.pageRoots, 40)
  assert.equal(result.visiblePageRoots, 32)
  assert.equal(inspected, 32)
  assert.equal(result.surfaceColor, 'rgb(1, 2, 3)')
  assert.equal(result.viewTransitionsAvailable, false)
})

test('render preference history is bounded, disabled outside developer mode and disposed with its owner', async () => {
  const exports = {}, logs = []
  let provider
  vm.runInNewContext(transpile('../src/features/diagnostics/renderState.ts'), { exports,
    require: id => id === 'vue' ? { watch } : id.includes('renderDiagnostics')
      ? { captureRenderDiagnostics: value => value } : id.includes('windowState')
        ? { useDiagnosticWindowState: (kind, snapshot) => { assert.equal(kind, 'renderer'); provider = snapshot } }
        : { createLogger: () => ({ info: (...args) => logs.push(args) }) },
  })
  const prefs = reactive({ theme: 'light', resolvedTheme: 'light', animations: true, scale: 1, locale: 'en', routePath: '/debug?private=hidden' })
  const setting = reactive({ enabled: false })
  const scope = effectScope()
  scope.run(() => exports.useRenderDiagnosticState(() => ({ ...prefs }), () => setting.enabled))
  prefs.theme = 'dark'; await nextTick()
  assert.equal(provider().changes.length, 0)
  assert.equal(logs.length, 0)
  setting.enabled = true; await nextTick()
  for (let index = 0; index < 25; index++) { prefs.routePath = '/models?private=' + index; await nextTick() }
  assert.equal(provider().changes.length, 20)
  assert.equal(logs.length, 1, 'route-only changes do not create noisy rendering logs')
  assert.ok(!JSON.stringify(provider().changes).includes('private'))
  prefs.animations = false; await nextTick()
  assert.equal(logs.length, 2)
  setting.enabled = false; await nextTick()
  assert.equal(provider().changes.length, 0)
  scope.stop()
  setting.enabled = true; prefs.theme = 'light'; await nextTick()
  assert.equal(provider().changes.length, 0)
})
