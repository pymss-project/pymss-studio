import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'

// Theme state reads the color scheme when the module is first imported.
const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
let runRippleViewTransition: typeof import('../src/utils/theme.ts').runRippleViewTransition
try {
  Object.defineProperty(globalThis, 'window', {
    value: { matchMedia: () => ({ matches: false }) }, configurable: true,
  })
  ;({ runRippleViewTransition } = await import('../src/utils/theme.ts'))
} finally {
  if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow)
  else Reflect.deleteProperty(globalThis, 'window')
}

const webkitUserAgent = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko) pymss-studio/0.0.25 Safari/605.1.15'
const origin = { x: 100, y: 200 }

function mockBrowser(t: TestContext, platform: string, userAgent: string) {
  const calls = { transitions: 0, animations: 0 }
  const document = {
    startViewTransition: ((update: () => void | Promise<void>) => {
      calls.transitions += 1
      return { ready: Promise.resolve().then(update) }
    }) as ((update: () => void | Promise<void>) => { ready: Promise<void> }) | undefined,
    documentElement: { animate: () => { calls.animations += 1 } },
  }
  for (const [key, value] of Object.entries({
    navigator: { platform, userAgent },
    window: { innerWidth: 1280, innerHeight: 720 },
    document,
  })) {
    const original = Object.getOwnPropertyDescriptor(globalThis, key)
    Object.defineProperty(globalThis, key, { value, configurable: true })
    t.after(() => {
      if (original) Object.defineProperty(globalThis, key, original)
      else Reflect.deleteProperty(globalThis, key)
    })
  }
  return { calls, document }
}

for (const platform of ['Linux x86_64', 'Linux aarch64']) {
  test(`Linux WebKit applies updates without invoking native view transitions (${platform})`, async t => {
    const { calls } = mockBrowser(t, platform, webkitUserAgent)
    let updates = 0
    await runRippleViewTransition(() => { updates += 1 }, origin)
    assert.equal(updates, 1)
    assert.deepEqual(calls, { transitions: 0, animations: 0 })
  })
}

for (const [name, platform, userAgent] of [
  ['Chrome on Linux', 'Linux x86_64', 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36'],
  ['headless Chrome on Linux', 'Linux x86_64', 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/142.0.0.0 Safari/537.36'],
  ['Chromium on Linux', 'Linux x86_64', 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chromium/142.0.0.0 Safari/537.36'],
  ['Firefox on Linux', 'Linux x86_64', 'Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0'],
  ['Safari on macOS', 'MacIntel', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15'],
  ['Edge on Windows', 'Win32', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0'],
  ['Android WebView', 'Linux armv8l', 'Mozilla/5.0 (Linux; Android 15; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/142.0.0.0 Mobile Safari/537.36'],
  ['Android WebKit', 'Linux armv7l', 'Mozilla/5.0 (Linux; U; Android 4.3) AppleWebKit/534.30 (KHTML, like Gecko) Version/4.0 Mobile Safari/534.30'],
] as const) {
  test(`supported engines retain the ripple transition: ${name}`, async t => {
    const { calls } = mockBrowser(t, platform, userAgent)
    let updates = 0
    await runRippleViewTransition(() => { updates += 1 }, origin)
    assert.equal(updates, 1)
    assert.deepEqual(calls, { transitions: 1, animations: 1 })
  })
}

test('the Linux WebKit fallback awaits asynchronous navigation exactly once', async t => {
  const { calls } = mockBrowser(t, 'Linux x86_64', webkitUserAgent)
  let finishUpdate!: () => void
  const pendingUpdate = new Promise<void>(resolve => { finishUpdate = resolve })
  let updates = 0
  let completed = false
  const pendingTransition = runRippleViewTransition(async () => {
    updates += 1
    await pendingUpdate
  }, origin).then(() => { completed = true })

  await Promise.resolve()
  assert.equal(updates, 1)
  assert.equal(completed, false)
  finishUpdate()
  await pendingTransition
  assert.equal(completed, true)
  assert.equal(updates, 1)
  assert.deepEqual(calls, { transitions: 0, animations: 0 })
})

test('the Linux WebKit fallback propagates update failures to the caller', async t => {
  const { calls } = mockBrowser(t, 'Linux x86_64', webkitUserAgent)
  const error = new Error('Navigation failed')
  await assert.rejects(runRippleViewTransition(async () => { throw error }, origin), error)
  assert.deepEqual(calls, { transitions: 0, animations: 0 })
})

for (const missing of ['origin', 'API']) {
  test(`updates still run directly without a transition ${missing}`, async t => {
    const { calls, document } = mockBrowser(t, 'Win32', 'AppleWebKit/537.36 Chrome/142.0.0.0')
    if (missing === 'API') document.startViewTransition = undefined
    let updates = 0
    await runRippleViewTransition(() => { updates += 1 }, missing === 'origin' ? undefined : origin)
    assert.equal(updates, 1)
    assert.deepEqual(calls, { transitions: 0, animations: 0 })
  })
}
