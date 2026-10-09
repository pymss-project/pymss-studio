import type { LGraphCanvas } from '@comfyorg/litegraph'

/** Complete LiteGraph 0.17.2's DPI handling for the full-size 2D editor. */
export function installCanvasDpi(renderer: LGraphCanvas) {
  const element = renderer.canvas
  const view = window
  const originalResize = renderer.resize
  const originalVisibleArea = renderer.ds.computeVisibleArea
  const originalFront = renderer.drawFrontCanvas
  const originalBack = renderer.drawBackCanvas
  const originalPrompt = renderer.prompt
  const originalDialog = renderer.createDialog
  const readRatio = () => {
    const ratio = view.devicePixelRatio
    return Number.isFinite(ratio) && ratio > 0 ? ratio : 1
  }
  let lastRatio = readRatio()

  // The editor's CSS controls display size; only the backing stores use device pixels.
  renderer.resize = function (width, height) {
    const rect = element.getBoundingClientRect()
    const ratio = readRatio()
    const pixelWidth = Math.max(0, Math.round((width ?? rect.width) * ratio))
    const pixelHeight = Math.max(0, Math.round((height ?? rect.height) * ratio))
    if (element.width === pixelWidth && element.height === pixelHeight
      && this.bgcanvas.width === pixelWidth && this.bgcanvas.height === pixelHeight
      && lastRatio === ratio) return
    if (element.width !== pixelWidth) element.width = pixelWidth
    if (element.height !== pixelHeight) element.height = pixelHeight
    if (this.bgcanvas.width !== pixelWidth) this.bgcanvas.width = pixelWidth
    if (this.bgcanvas.height !== pixelHeight) this.bgcanvas.height = pixelHeight
    lastRatio = ratio
    this.setDirty(true, true)
  }

  renderer.ds.computeVisibleArea = function (viewport) {
    const ratio = readRatio()
    originalVisibleArea.call(this, viewport ?? [0, 0, element.width / ratio, element.height / ratio])
  }

  renderer.drawFrontCanvas = function () {
    const ctx = this.ctx
    const ratio = readRatio()
    const setTransform = ctx.setTransform
    ctx.save()
    // Vendor clearRect uses backing dimensions. Below DPR 1 its transformed clear
    // cannot cover the whole buffer; clear the full frame in device coordinates first.
    if (ratio < 1 && this.clear_background && !this.viewport && !this.dirty_area) {
      ctx.setTransform(1, 0, 0, 1, 0, 0)
      ctx.clearRect(0, 0, element.width, element.height)
    }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0)
    // LiteGraph clamps the selection rectangle's transform to 1 even when webview zoom
    // makes DPR smaller than 1. Interpret that reset as CSS coordinates too.
    if (ratio < 1) {
      ctx.setTransform = ((...args: unknown[]) => {
        if (args.length === 6 && args[0] === 1 && args[1] === 0 && args[2] === 0
          && args[3] === 1 && args[4] === 0 && args[5] === 0) {
          Reflect.apply(setTransform, ctx, [ratio, 0, 0, ratio, 0, 0])
        } else {
          Reflect.apply(setTransform, ctx, args)
        }
      }) as typeof ctx.setTransform
    }
    try {
      // Background rendering already applies DPR and front compositing divides it out.
      originalFront.call(this)
    } finally {
      ctx.setTransform = setTransform
      ctx.restore()
    }
  }

  renderer.drawBackCanvas = function () {
    // Background clearRect precedes the vendor's DPR transform, which otherwise
    // remains active from the previous frame and also under-clears at DPR < 1.
    const ctx = this.bgctx || this.bgcanvas.getContext('2d')
    ctx?.setTransform(1, 0, 0, 1, 0, 0)
    originalBack.call(this)
  }

  // Vendor fallback positions use backing dimensions; DOM dialogs need CSS coordinates.
  renderer.prompt = function (...args) {
    const dialog = originalPrompt.apply(this, args)
    if (!args[3]) {
      const rect = element.getBoundingClientRect()
      dialog.style.left = `${rect.width / 2 - 20}px`
      dialog.style.top = `${rect.height / 2 - 20}px`
    }
    return dialog
  }
  renderer.createDialog = function (html, options) {
    if (!options?.position && !options?.event) {
      const rect = element.getBoundingClientRect()
      options = { ...options, position: [rect.left + rect.width / 2, rect.top + rect.height / 2] }
    }
    return originalDialog.call(this, html, options)
  }

  const resize = () => renderer.resize()
  let resolutionQuery: MediaQueryList | null = null
  const onResolutionChange = () => {
    resize()
    watchResolution()
  }
  function watchResolution() {
    resolutionQuery?.removeEventListener('change', onResolutionChange)
    resolutionQuery = view.matchMedia(`(resolution: ${readRatio()}dppx)`)
    resolutionQuery.addEventListener('change', onResolutionChange)
  }
  const observer = new ResizeObserver(resize)
  observer.observe(element.parentElement || element)
  view.addEventListener('resize', resize)
  view.visualViewport?.addEventListener('resize', resize)
  watchResolution()
  resize()

  return () => {
    observer.disconnect()
    resolutionQuery?.removeEventListener('change', onResolutionChange)
    view.removeEventListener('resize', resize)
    view.visualViewport?.removeEventListener('resize', resize)
    renderer.resize = originalResize
    renderer.ds.computeVisibleArea = originalVisibleArea
    renderer.drawFrontCanvas = originalFront
    renderer.drawBackCanvas = originalBack
    renderer.prompt = originalPrompt
    renderer.createDialog = originalDialog
  }
}
