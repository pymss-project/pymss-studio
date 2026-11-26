import { onBeforeUnmount, toValue, watch, type MaybeRefOrGetter, type Ref } from 'vue'
import type { useEditorStore } from '@/stores/editor'

type EditorStore = ReturnType<typeof useEditorStore>

type UseEditorMixerViewOptions = {
  editor: EditorStore
  trackHeaderWidth: MaybeRefOrGetter<number>
  scrollEl: Ref<HTMLElement | null>
  playbackLoop: Ref<boolean>
}

export function useEditorMixerView(options: UseEditorMixerViewOptions) {
  const { editor, trackHeaderWidth, scrollEl, playbackLoop } = options
  let resizeObserver: ResizeObserver | null = null

  function minZoomForViewport() {
    const viewportWidth = scrollEl.value?.clientWidth || 0
    if (!viewportWidth || editor.duration <= 0) return 4
    const laneViewportWidth = Math.max(1, viewportWidth - toValue(trackHeaderWidth))
    return Math.max(4, laneViewportWidth / Math.max(editor.duration, 0.01))
  }

  function constrainZoom(value: number) {
    const minZoom = minZoomForViewport()
    return Math.min(240, Math.max(minZoom, value))
  }

  // A timeline showing the whole project keeps doing so when the lane area resizes.
  // Duration changes never refit: recording and clip edits would rescale the view while working.
  let lastMinZoom: number | null = null
  function ensureMinimumZoom(refitWhenFitted = false) {
    if (editor.duration <= 0) return
    const minZoom = minZoomForViewport()
    const fitted = refitWhenFitted && lastMinZoom !== null && Math.abs(editor.pixelsPerSecond - lastMinZoom) < 0.001
    lastMinZoom = minZoom
    if (editor.pixelsPerSecond < minZoom || fitted) {
      editor.setZoom(minZoom)
    }
  }

  function zoomFit() {
    if (editor.duration <= 0) return
    editor.setZoom(constrainZoom(minZoomForViewport()))
  }

  function zoomAt(payload: { direction?: 'in' | 'out'; anchorRatio: number; deltaY?: number }) {
    const element = scrollEl.value
    const currentZoom = editor.pixelsPerSecond
    const wheelDelta = payload.deltaY ?? (payload.direction === 'in' ? -120 : 120)
    const zoomFactor = Math.exp(-wheelDelta * 0.0016)
    const nextZoom = constrainZoom(currentZoom * zoomFactor)

    if (!element) {
      editor.setZoom(nextZoom)
      return
    }

    const anchorX = element.scrollLeft + element.clientWidth * payload.anchorRatio - toValue(trackHeaderWidth)
    const anchorTime = Math.max(0, anchorX / Math.max(1, currentZoom))
    editor.setZoom(nextZoom)
    requestAnimationFrame(() => {
      element.scrollLeft = Math.max(
        0,
        toValue(trackHeaderWidth) + anchorTime * editor.pixelsPerSecond - element.clientWidth * payload.anchorRatio,
      )
    })
  }

  function updatePlaybackLoop(value: boolean) {
    playbackLoop.value = value
  }

  function handleMixerScrollReady(element: HTMLElement) {
    scrollEl.value = element
  }

  watch(
    () => scrollEl.value,
    (element, previous) => {
      if (resizeObserver && previous) {
        resizeObserver.unobserve(previous)
      }
      if (!element || typeof ResizeObserver === 'undefined') return
      resizeObserver = new ResizeObserver(() => {
        ensureMinimumZoom(true)
      })
      resizeObserver.observe(element)
      ensureMinimumZoom()
    },
    { immediate: true },
  )

  watch(
    () => editor.duration,
    () => {
      ensureMinimumZoom()
    },
    { immediate: true },
  )

  watch(() => toValue(trackHeaderWidth), () => ensureMinimumZoom(true))

  onBeforeUnmount(() => {
    resizeObserver?.disconnect()
    resizeObserver = null
  })

  return {
    zoomFit,
    zoomAt,
    ensureMinimumZoom,
    zoomIn: () => editor.setZoom(constrainZoom(editor.pixelsPerSecond * 1.18)),
    zoomOut: () => editor.setZoom(constrainZoom(editor.pixelsPerSecond / 1.18)),
    updatePlaybackLoop,
    handleMixerScrollReady,
  }
}
