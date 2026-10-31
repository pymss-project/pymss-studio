const refreshEvent = 'pymss:layout-refresh'

/** Resend the natural height after the host has stopped ignoring overlay resizes. */
export function refreshToolLayout() {
  window.dispatchEvent(new Event(refreshEvent))
}

/** Keep the sandbox's natural content height aligned with its Studio container. */
export function observeToolLayout() {
  let disposed = false
  let frame = 0
  let previous = 0
  let popupSpace = 0
  const observedPopups = new Set<HTMLElement>()
  const measure = () => {
    frame = 0
    if (disposed) return
    // Teleported menus are outside normal flow, but still clipped by the iframe.
    // Reserve space only after a floating control is clipped, and keep it until
    // dismissal so the follower does not oscillate between above/below placement.
    const popups = [...document.querySelectorAll<HTMLElement>('.v-binder-follower-content')]
      .filter(element => element.getClientRects().length && element.getBoundingClientRect().height > 0 && getComputedStyle(element).visibility !== 'hidden')
    for (const element of observedPopups) {
      if (!popups.includes(element)) { observer.unobserve(element); observedPopups.delete(element) }
    }
    if (!popups.length) popupSpace = 0
    for (const element of popups) {
      if (!observedPopups.has(element)) { observer.observe(element); observedPopups.add(element) }
      const box = element.getBoundingClientRect()
      if (box.height > 0 && (box.top < 0 || box.bottom > window.innerHeight)) {
        popupSpace = Math.max(popupSpace, Math.ceil(box.height) + 16)
      }
    }
    const height = Math.ceil(document.body.getBoundingClientRect().height) + popupSpace
    if (!Number.isFinite(height) || height <= 0) return
    const bounded = Math.max(120, Math.min(20000, height))
    if (bounded === previous) return
    previous = bounded
    window.parent.postMessage({ jsonrpc: '2.0', method: 'pymss.resize', params: { height: bounded } }, '*')
  }
  const schedule = () => { if (!disposed && !frame) frame = requestAnimationFrame(measure) }
  const refresh = () => { previous = 0; schedule() }
  const observer = new ResizeObserver(schedule)
  const mutations = new MutationObserver(schedule)
  observer.observe(document.body)
  mutations.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style', 'hidden'] })
  window.addEventListener('resize', schedule)
  window.addEventListener(refreshEvent, refresh)
  void document.fonts?.ready.then(schedule)
  schedule()
  return () => {
    disposed = true
    observer.disconnect()
    mutations.disconnect()
    observedPopups.clear()
    cancelAnimationFrame(frame)
    window.removeEventListener('resize', schedule)
    window.removeEventListener(refreshEvent, refresh)
  }
}
