import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'
import type { LGraphCanvas } from '@comfyorg/litegraph'
import { installCanvasDpi } from '../src/litegraph/canvasDpi.ts'

function fixture(t: TestContext, ratio: number) {
  const listeners = new Map<string, Set<() => void>>()
  const viewportListeners = new Set<() => void>()
  const queries: Array<{ listeners: Set<() => void> }> = []
  const screen = {
    devicePixelRatio: ratio,
    addEventListener: (type: string, callback: () => void) => {
      if (!listeners.has(type)) listeners.set(type, new Set())
      listeners.get(type)!.add(callback)
    },
    removeEventListener: (type: string, callback: () => void) => listeners.get(type)?.delete(callback),
    visualViewport: {
      addEventListener: (_: string, callback: () => void) => viewportListeners.add(callback),
      removeEventListener: (_: string, callback: () => void) => viewportListeners.delete(callback),
    },
    matchMedia: () => {
      const query = {
        listeners: new Set<() => void>(),
        addEventListener: (_: string, callback: () => void) => query.listeners.add(callback),
        removeEventListener: (_: string, callback: () => void) => query.listeners.delete(callback),
      }
      queries.push(query)
      return query
    },
  }
  let disconnected = false
  let resizeCallback: () => void = () => {}
  const globals = ['window', 'ResizeObserver'].map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)] as const)
  Object.defineProperty(globalThis, 'window', { value: screen, configurable: true })
  Object.defineProperty(globalThis, 'ResizeObserver', { configurable: true, value: class {
    constructor(callback: () => void) { resizeCallback = callback }
    observe() {}
    disconnect() { disconnected = true }
  } })
  t.after(() => {
    for (const [name, descriptor] of globals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else Reflect.deleteProperty(globalThis, name)
    }
  })
  const rect = { left: 12, top: 40, width: 320, height: 180 }
  const element = { width: 300, height: 150, parentElement: {}, getBoundingClientRect: () => rect }
  let matrix = [1, 0, 0, 1, 0, 0]
  const stack: number[][] = []
  const clears: Array<{ scale: number, width: number, height: number }> = []
  const ctx = {
    save() { stack.push([...matrix]) },
    restore() { matrix = stack.pop()! },
    setTransform(...args: unknown[]) {
      matrix = args.length === 6 ? args as number[] : Object.values(args[0] as Record<string, number>)
    },
    clearRect(_x: number, _y: number, width: number, height: number) {
      clears.push({ scale: matrix[0], width, height })
    },
  }
  let backRatio = 1
  const backClears: number[] = []
  const bgctx = {
    setTransform(ratio: number) { backRatio = ratio },
    clearRect() { backClears.push(backRatio) },
  }
  const draws: number[][] = []
  let failDrawing = false
  let dirtyCount = 0
  const renderer = {
    canvas: element, bgcanvas: { width: 300, height: 150, getContext: () => bgctx }, ctx, bgctx,
    clear_background: true,
    ds: { viewport: [] as number[], computeVisibleArea(viewport: number[]) { this.viewport = viewport } },
    resize() {},
    drawFrontCanvas() {
      ctx.clearRect(0, 0, element.width, element.height)
      draws.push([...matrix])
      // The library resets selection rectangles to max(1, DPR).
      const selectionRatio = Math.max(1, screen.devicePixelRatio)
      ctx.setTransform(selectionRatio, 0, 0, selectionRatio, 0, 0)
      draws.push([...matrix])
      if (failDrawing) throw new Error('Draw failed')
    },
    drawBackCanvas() {
      bgctx.clearRect()
      bgctx.setTransform(screen.devicePixelRatio)
    },
    setDirty(front: boolean, back: boolean) {
      assert.equal(front && back, true)
      dirtyCount++
    },
    prompt() { return { style: { left: '', top: '' } } },
    createDialog(_html: string, options: unknown) { return { options } },
  } as unknown as LGraphCanvas
  const originals = { resize: renderer.resize, area: renderer.ds.computeVisibleArea, front: renderer.drawFrontCanvas, back: renderer.drawBackCanvas,
    prompt: renderer.prompt, dialog: renderer.createDialog, transform: ctx.setTransform }
  const dispose = installCanvasDpi(renderer)
  t.after(dispose)
  return { renderer, screen, element, rect, draws, clears, backClears, ctx, stack, queries, originals, dispose, listeners, viewportListeners,
    resize: () => resizeCallback(), dpiChange(value: number) {
      screen.devicePixelRatio = value
      for (const callback of [...queries.at(-1)!.listeners]) callback()
    }, failDrawing: () => { failDrawing = true }, dirtyCount: () => dirtyCount,
    disconnected: () => disconnected }
}

test('backing pixels scale with DPR while visible bounds and drawing use CSS coordinates', async t => {
  for (const ratio of [0.75, 1, 1.25, 1.5, 2]) {
    await t.test(`DPR ${ratio}`, t => {
      const env = fixture(t, ratio)
      assert.equal(env.element.width, Math.round(320 * ratio))
      assert.equal(env.element.height, Math.round(180 * ratio))
      assert.equal(env.renderer.bgcanvas.width, env.element.width)
      env.renderer.ds.computeVisibleArea(undefined)
      assert.deepEqual((env.renderer.ds as unknown as { viewport: number[] }).viewport, [0, 0, 320, 180])
      env.renderer.drawFrontCanvas()
      assert.deepEqual(env.draws, [[ratio, 0, 0, ratio, 0, 0], [ratio, 0, 0, ratio, 0, 0]])
      assert.equal(env.ctx.setTransform, env.originals.transform)
      assert.equal(env.stack.length, 0)
      assert.equal(env.screen.devicePixelRatio, ratio)
      assert.ok(env.clears.some(clear => clear.width * clear.scale >= env.element.width
        && clear.height * clear.scale >= env.element.height))
      env.renderer.drawBackCanvas()
      env.renderer.drawBackCanvas()
      assert.deepEqual(env.backClears, [1, 1])
    })
  }
})

test('DPI changes without layout resize reallocate both buffers and rearm the resolution observer', t => {
  const env = fixture(t, 1.25)
  env.dpiChange(2)
  assert.equal(env.element.width, 640)
  assert.equal(env.element.height, 360)
  assert.equal(env.renderer.bgcanvas.width, 640)
  assert.equal(env.queries[0].listeners.size, 0)
  assert.equal(env.queries[1].listeners.size, 1)
  const before = env.dirtyCount()
  env.renderer.resize()
  assert.equal(env.dirtyCount(), before)
  env.rect.width = 400
  env.resize()
  assert.equal(env.element.width, 800)
})

test('explicit viewports and DOM dialog positions remain in logical pixels', t => {
  const env = fixture(t, 2)
  const viewport: [number, number, number, number] = [10, 20, 100, 80]
  env.renderer.ds.computeVisibleArea(viewport)
  assert.equal((env.renderer.ds as unknown as { viewport: number[] }).viewport, viewport)
  const dialog = env.renderer.createDialog('Settings', {}) as unknown as { options: { position: number[] } }
  assert.deepEqual(dialog.options.position, [172, 130])
  const options = { position: [42, 50] as [number, number] }
  const placed = env.renderer.createDialog('Settings', options) as unknown as { options: unknown }
  assert.equal(placed.options, options)
  const prompt = env.renderer.prompt('Name', '', () => {}, undefined as never)
  assert.equal(prompt.style.left, '140px')
  assert.equal(prompt.style.top, '70px')
})

test('draw failures restore the context and disposal restores methods and releases observers', t => {
  const env = fixture(t, 0.75)
  env.failDrawing()
  assert.throws(() => env.renderer.drawFrontCanvas(), /Draw failed/)
  assert.equal(env.ctx.setTransform, env.originals.transform)
  assert.equal(env.stack.length, 0)
  env.dispose()
  assert.equal(env.renderer.resize, env.originals.resize)
  assert.equal(env.renderer.ds.computeVisibleArea, env.originals.area)
  assert.equal(env.renderer.drawFrontCanvas, env.originals.front)
  assert.equal(env.renderer.drawBackCanvas, env.originals.back)
  assert.equal(env.renderer.prompt, env.originals.prompt)
  assert.equal(env.renderer.createDialog, env.originals.dialog)
  assert.equal(env.disconnected(), true)
  assert.equal(env.listeners.get('resize')!.size, 0)
  assert.equal(env.viewportListeners.size, 0)
  assert.equal(env.queries.at(-1)!.listeners.size, 0)
})
