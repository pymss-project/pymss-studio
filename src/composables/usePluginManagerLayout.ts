import { computed, onMounted, onScopeDispose, ref, watch, type Ref } from 'vue'

type Split = 'library' | 'parameters'
const STORAGE_KEY = 'pymss-plugin-manager-layout-v1'
const HANDLE_SIZE = 8
const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), max)

export function usePluginManagerLayout(open: Ref<boolean>, parametersOpen: Ref<boolean>) {
  const managerBody = ref<HTMLElement | null>(null)
  const managerChain = ref<HTMLElement | null>(null)
  const preferredLibrary = ref<number>()
  const preferredParameters = ref<number>()
  const bodyWidth = ref(0)
  const paneHeight = ref(0)
  const viewportHeight = ref(window.innerHeight)
  const resizing = ref<Split | null>(null)
  const compact = computed(() => bodyWidth.value > 0 && bodyWidth.value < 720)
  const libraryMin = computed(() => Math.min(260, Math.max(0, bodyWidth.value - HANDLE_SIZE)))
  const libraryMax = computed(() => Math.max(libraryMin.value, bodyWidth.value - HANDLE_SIZE - 380))
  const libraryWidth = computed(() => clamp(preferredLibrary.value ?? clamp(bodyWidth.value * 0.35, 280, 380), libraryMin.value, libraryMax.value))
  // Keep the toolbar, parameter list and pagination usable. When both panes no
  // longer fit, the chain scrolls instead of reducing the dock below its minimum.
  const parameterMin = computed(() => 150)
  const parameterMax = computed(() => Math.max(parameterMin.value, paneHeight.value - 96))
  const parameterHeight = computed(() => clamp(preferredParameters.value
    ?? Math.min(clamp(viewportHeight.value * 0.32, 210, 360), paneHeight.value * 0.55), parameterMin.value, parameterMax.value))
  const managerStyle = computed(() => ({
    ...(bodyWidth.value > 0 ? { '--plugin-library-width': `${libraryWidth.value}px` } : {}),
    ...(parametersOpen.value ? { '--plugin-parameter-height': `${parameterHeight.value}px` } : {}),
  }))
  let resizeObserver: ResizeObserver | null = null
  let mutationObserver: MutationObserver | null = null
  let disposed = false
  let drag: { kind: Split; id: number; element: HTMLElement; coordinate: number; lastCoordinate: number; size: number; preference: number | undefined } | null = null

  function measure() {
    if (disposed) return
    viewportHeight.value = window.innerHeight
    if (!open.value) return
    const body = managerBody.value
    const chain = managerChain.value
    if (body) bodyWidth.value = body.clientWidth
    if (chain?.clientHeight && parametersOpen.value) {
      const style = getComputedStyle(chain)
      const children = Array.from(chain.children) as HTMLElement[]
      const fixed = children.filter(child => !child.matches('.plugin-manager__slots, .plugin-manager__parameter-dock, [role="separator"]'))
        .reduce((total, child) => total + child.offsetHeight, 0)
      const padding = (Number.parseFloat(style.paddingTop) || 0) + (Number.parseFloat(style.paddingBottom) || 0)
      const gaps = (Number.parseFloat(style.rowGap) || 0) * Math.max(0, children.length - 1)
      paneHeight.value = Math.max(0, chain.clientHeight - padding - fixed - gaps - HANDLE_SIZE)
    }
    if (compact.value && resizing.value === 'library') finishResize()
  }

  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ libraryWidth: preferredLibrary.value, parameterHeight: preferredParameters.value }))
    } catch { /* Layout preferences must not interrupt plugin operations. */ }
  }

  function preference(kind: Split) { return kind === 'library' ? preferredLibrary : preferredParameters }
  function currentSize(kind: Split) { return kind === 'library' ? libraryWidth.value : parameterHeight.value }
  function bounded(kind: Split, value: number) {
    return kind === 'library' ? clamp(value, libraryMin.value, libraryMax.value) : clamp(value, parameterMin.value, parameterMax.value)
  }

  function moveResize(event: PointerEvent) {
    if (!drag || event.pointerId !== drag.id) return
    drag.lastCoordinate = drag.kind === 'library' ? event.clientX : event.clientY
    const delta = drag.kind === 'library' ? event.clientX - drag.coordinate : drag.coordinate - event.clientY
    const next = bounded(drag.kind, drag.size + delta)
    if (next !== currentSize(drag.kind)) preference(drag.kind).value = next
  }

  function finishResize(cancel = false) {
    const current = drag
    if (!current) return
    drag = null
    resizing.value = null
    window.removeEventListener('pointermove', moveResize)
    window.removeEventListener('pointerup', releaseResize)
    window.removeEventListener('pointercancel', cancelResize)
    window.removeEventListener('blur', blurResize)
    if (cancel) preference(current.kind).value = current.preference
    else if (preference(current.kind).value !== current.preference) persist()
    try {
      if (current.element.hasPointerCapture(current.id)) current.element.releasePointerCapture(current.id)
    } catch { /* The separator may have been unmounted during a project switch. */ }
  }
  function releaseResize(event: PointerEvent) { if (drag?.id === event.pointerId) finishResize() }
  function cancelResize(event: PointerEvent) { if (drag?.id === event.pointerId) finishResize(true) }
  function blurResize() { finishResize() }

  function startResize(kind: Split, event: PointerEvent) {
    if (event.button !== 0 || !open.value || (kind === 'library' && compact.value)
      || (kind === 'parameters' && !parametersOpen.value)) return
    measure()
    finishResize()
    const element = event.currentTarget as HTMLElement
    event.preventDefault()
    event.stopPropagation()
    element.focus({ preventScroll: true })
    drag = { kind, id: event.pointerId, element, coordinate: kind === 'library' ? event.clientX : event.clientY,
      lastCoordinate: kind === 'library' ? event.clientX : event.clientY,
      size: currentSize(kind), preference: preference(kind).value }
    resizing.value = kind
    try { element.setPointerCapture(event.pointerId) } catch { /* Window listeners also cover mouse capture fallback. */ }
    window.addEventListener('pointermove', moveResize)
    window.addEventListener('pointerup', releaseResize)
    window.addEventListener('pointercancel', cancelResize)
    window.addEventListener('blur', blurResize)
  }

  function resetSize(kind: Split) {
    finishResize(true)
    preference(kind).value = undefined
    persist()
  }

  function resizeWithKeyboard(kind: Split, event: KeyboardEvent) {
    if (event.key === 'Escape' && drag) {
      finishResize(true)
      event.preventDefault()
      event.stopPropagation()
      return
    }
    const decrease = kind === 'library' ? 'ArrowLeft' : 'ArrowDown'
    const increase = kind === 'library' ? 'ArrowRight' : 'ArrowUp'
    if (![decrease, increase, 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    event.stopPropagation()
    if (drag && drag.kind !== kind) finishResize()
    measure()
    const step = event.shiftKey ? 48 : 16
    const previous = currentSize(kind)
    const next = bounded(kind, event.key === 'Home' ? -Infinity : event.key === 'End' ? Infinity
      : previous + (event.key === increase ? step : -step))
    if (next !== previous) {
      preference(kind).value = next
      if (drag) {
        drag.size = next
        drag.coordinate = drag.lastCoordinate
      } else persist()
    }
  }

  watch([open, parametersOpen, managerBody, managerChain], () => {
    if (!open.value || (resizing.value === 'parameters' && !parametersOpen.value)) finishResize()
    resizeObserver?.disconnect()
    mutationObserver?.disconnect()
    if (!open.value) return
    if (typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(measure)
      if (managerBody.value) resizeObserver.observe(managerBody.value)
      if (managerChain.value) resizeObserver.observe(managerChain.value)
    }
    if (typeof MutationObserver !== 'undefined' && managerChain.value) {
      mutationObserver = new MutationObserver(measure)
      mutationObserver.observe(managerChain.value, { childList: true })
    }
    measure()
  }, { flush: 'post' })

  onMounted(() => {
    try {
      const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
      if (stored && typeof stored === 'object') {
        if (typeof stored.libraryWidth === 'number' && Number.isFinite(stored.libraryWidth) && stored.libraryWidth > 0) preferredLibrary.value = stored.libraryWidth
        if (typeof stored.parameterHeight === 'number' && Number.isFinite(stored.parameterHeight) && stored.parameterHeight > 0) preferredParameters.value = stored.parameterHeight
      }
    } catch { /* Invalid preferences fall back to the responsive defaults. */ }
    window.addEventListener('resize', measure)
    measure()
  })
  onScopeDispose(() => {
    disposed = true
    finishResize()
    resizeObserver?.disconnect()
    mutationObserver?.disconnect()
    window.removeEventListener('resize', measure)
  })

  return { managerBody, managerChain, managerStyle, libraryWidth, libraryMin, libraryMax,
    parameterHeight, parameterMin, parameterMax, resizing, compact, startResize, resizeWithKeyboard, resetSize }
}
