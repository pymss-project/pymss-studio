import { computed, onBeforeUnmount, onMounted, ref, type Ref } from 'vue'
import { loadAppStore, saveAppStore } from '@/utils/appStore'
import { calculateEditorPanelCapacity, resolveEditorPanelLayout } from '@/utils/editorLayout'

type ResizeSide = 'assets' | 'inspector'

export const TRACK_HEADER_DEFAULT_WIDTH = 180
export const TRACK_HEADER_MIN_WIDTH = 150
export const TRACK_HEADER_MAX_WIDTH = 360
export const MIXER_PANEL_MIN_HEIGHT = 260
export const MIXER_PANEL_MAX_HEIGHT = 560

function clampTrackHeaderWidth(width: number) {
  return Math.round(Math.min(TRACK_HEADER_MAX_WIDTH, Math.max(TRACK_HEADER_MIN_WIDTH, width)))
}

function clampMixerPanelHeight(height: number) {
  return Math.round(Math.min(MIXER_PANEL_MAX_HEIGHT, Math.max(MIXER_PANEL_MIN_HEIGHT, height)))
}

type ResizeState = {
  side: ResizeSide
  startX: number
  startAssetWidth: number
  startInspectorWidth: number
}

type UseEditorLayoutOptions = {
  shellEl: Ref<HTMLElement | null>
  assetRailWidth: number
  inspectorRailWidth: number
  resizerWidth: number
  minAssetWidth: number
  maxAssetWidth: number
  minCenterWidth: number
  minInspectorWidth: number
  maxInspectorWidth: number
  initialAssetWidth: number
  initialInspectorWidth?: number
}

type EditorUiState = {
  assetCollapsed?: boolean
  inspectorCollapsed?: boolean
  mixerCollapsed?: boolean
  trackHeaderWidth?: number
  mixerPanelHeight?: number | null
  assetPanelWidth?: number
  inspectorPanelWidth?: number
  panelPriority?: ResizeSide
}

export function useEditorLayout(options: UseEditorLayoutOptions) {
  const {
    shellEl,
    assetRailWidth,
    inspectorRailWidth,
    resizerWidth,
    minAssetWidth,
    maxAssetWidth,
    minCenterWidth,
    minInspectorWidth,
    maxInspectorWidth,
    initialAssetWidth,
    initialInspectorWidth = 288,
  } = options

  const viewportWidth = ref(typeof window !== 'undefined' ? window.innerWidth : 1440)
  const preferredInspectorWidth = ref<number | undefined>()
  const preferredAssetWidth = ref(initialAssetWidth)
  const panelPriority = ref<ResizeSide>('inspector')
  const isAssetCollapsed = ref(false)
  const isInspectorCollapsed = ref(true)
  const isMixerCollapsed = ref(true)
  const trackHeaderWidth = ref(TRACK_HEADER_DEFAULT_WIDTH)
  // null keeps the console at the natural height of its channel strips.
  const mixerPanelHeight = ref<number | null>(null)
  const activeResize = ref<ResizeState | null>(null)
  const initialized = ref(false)

  const layout = computed(() => resolveEditorPanelLayout({
    shellWidth: viewportWidth.value, minCenterWidth, assetRailWidth, inspectorRailWidth, resizerWidth,
    minAssetWidth, maxAssetWidth, minInspectorWidth, maxInspectorWidth,
    assetPanelWidth: preferredAssetWidth.value,
    inspectorPanelWidth: preferredInspectorWidth.value
      ?? clamp(Math.round(viewportWidth.value * 0.18), initialInspectorWidth, maxInspectorWidth),
    assetRequested: !isAssetCollapsed.value, inspectorRequested: !isInspectorCollapsed.value,
    priority: panelPriority.value,
  }))
  const inspectorPanelWidth = computed(() => layout.value.inspectorPanelWidth)
  const assetPanelWidth = computed(() => layout.value.assetPanelWidth)
  const inspectorVisible = computed(() => layout.value.inspectorVisible)
  const assetPanelVisible = computed(() => layout.value.assetPanelVisible)
  const assetResizerVisible = computed(() => assetPanelVisible.value)
  const inspectorPanelVisible = computed(() => layout.value.inspectorPanelVisible)

  const shellStyle = computed(() => ({
    '--asset-rail-width': `${assetRailWidth}px`,
    '--asset-panel-width': assetPanelVisible.value ? `${assetPanelWidth.value}px` : '0px',
    '--asset-resizer-width': assetResizerVisible.value ? `${resizerWidth}px` : '0px',
    // The inspector toggle lives inside the panel. Only reserve the narrow
    // launcher width while the panel is collapsed; an expanded inspector no
    // longer carries a separate rail column.
    '--inspector-rail-width': inspectorVisible.value && !inspectorPanelVisible.value
      ? `${inspectorRailWidth}px`
      : '0px',
    '--inspector-resizer-width': inspectorPanelVisible.value ? `${resizerWidth}px` : '0px',
    '--inspector-width': inspectorPanelVisible.value ? `${inspectorPanelWidth.value}px` : '0px',
  }))

  function clamp(value: number, min: number, max: number) {
    return Math.min(Math.max(value, min), max)
  }

  function getShellWidth() {
    return shellEl.value?.clientWidth || window.innerWidth || 0
  }

  function getPanelCapacity(overrides: {
    inspectorPanelVisible?: boolean
    inspectorRailVisible?: boolean
    assetPanelWidth?: number
    inspectorPanelWidth?: number
  } = {}) {
    const nextInspectorPanelVisible = overrides.inspectorPanelVisible ?? inspectorPanelVisible.value
    const nextInspectorRailVisible = overrides.inspectorRailVisible
      ?? (inspectorVisible.value && !nextInspectorPanelVisible)
    return calculateEditorPanelCapacity({
      shellWidth: getShellWidth(),
      minCenterWidth,
      assetRailWidth,
      inspectorRailWidth,
      resizerWidth,
      assetPanelWidth: overrides.assetPanelWidth ?? assetPanelWidth.value,
      inspectorPanelWidth: overrides.inspectorPanelWidth ?? inspectorPanelWidth.value,
      assetPanelVisible: assetPanelVisible.value,
      inspectorRailVisible: nextInspectorRailVisible,
      inspectorPanelVisible: nextInspectorPanelVisible,
    })
  }

  function clampInspectorWidth(width: number) {
    const responsiveMax = getPanelCapacity({
      assetPanelWidth: assetPanelVisible.value ? minAssetWidth : assetPanelWidth.value,
    }).availableInspectorWidth
    return clamp(width, minInspectorWidth, Math.max(minInspectorWidth, Math.min(maxInspectorWidth, responsiveMax)))
  }

  function clampAssetWidth(width: number) {
    const responsiveMax = getPanelCapacity({
      inspectorPanelWidth: inspectorPanelVisible.value ? minInspectorWidth : inspectorPanelWidth.value,
    }).availableAssetWidth
    return clamp(width, minAssetWidth, Math.max(minAssetWidth, Math.min(maxAssetWidth, responsiveMax)))
  }

  let saveTimer: ReturnType<typeof setTimeout> | null = null
  let observer: ResizeObserver | null = null
  let disposed = false

  function persistPanelWidths() {
    void saveAppStore('editor-ui', {
      assetCollapsed: isAssetCollapsed.value,
      inspectorCollapsed: isInspectorCollapsed.value,
      mixerCollapsed: isMixerCollapsed.value,
      trackHeaderWidth: trackHeaderWidth.value,
      mixerPanelHeight: mixerPanelHeight.value,
      assetPanelWidth: preferredAssetWidth.value,
      inspectorPanelWidth: preferredInspectorWidth.value,
      panelPriority: panelPriority.value,
    } satisfies EditorUiState)
  }

  function savePanelWidths() {
    if (!initialized.value) return
    if (saveTimer) clearTimeout(saveTimer)
    saveTimer = setTimeout(() => {
      saveTimer = null
      persistPanelWidths()
    }, 80)
  }

  function handleResizeMove(event: MouseEvent) {
    const state = activeResize.value
    if (!state) return
    if (state.side === 'assets') {
      const width = clampAssetWidth(state.startAssetWidth + (event.clientX - state.startX))
      if (width !== assetPanelWidth.value) {
        preferredAssetWidth.value = width
        panelPriority.value = 'assets'
      }
      return
    }
    const width = clampInspectorWidth(state.startInspectorWidth - (event.clientX - state.startX))
    if (width !== inspectorPanelWidth.value) {
      preferredInspectorWidth.value = width
      panelPriority.value = 'inspector'
    }
  }

  function stopResize() {
    if (!activeResize.value) return
    activeResize.value = null
    window.removeEventListener('mousemove', handleResizeMove)
    window.removeEventListener('mouseup', stopResize)
    savePanelWidths()
  }

  function startResize(side: ResizeSide, event: MouseEvent) {
    if (event.button !== 0) return
    if (side === 'assets' && !assetResizerVisible.value) return
    if (side === 'inspector' && !inspectorPanelVisible.value) return
    event.preventDefault()
    activeResize.value = {
      side,
      startX: event.clientX,
      startAssetWidth: assetPanelWidth.value,
      startInspectorWidth: inspectorPanelWidth.value,
    }
    window.addEventListener('mousemove', handleResizeMove)
    window.addEventListener('mouseup', stopResize)
  }

  async function restorePanelWidths() {
    const stored = await loadAppStore<EditorUiState>('editor-ui')
    if (disposed) return
    isAssetCollapsed.value = Boolean(stored?.assetCollapsed)
    isInspectorCollapsed.value = typeof stored?.inspectorCollapsed === 'boolean'
      ? stored.inspectorCollapsed
      : true
    isMixerCollapsed.value = stored?.mixerCollapsed !== false
    trackHeaderWidth.value = clampTrackHeaderWidth(Number(stored?.trackHeaderWidth) || TRACK_HEADER_DEFAULT_WIDTH)
    const storedConsoleHeight = Number(stored?.mixerPanelHeight)
    mixerPanelHeight.value = Number.isFinite(storedConsoleHeight) && storedConsoleHeight > 0 ? clampMixerPanelHeight(storedConsoleHeight) : null
    preferredAssetWidth.value = clamp(
      Number(stored?.assetPanelWidth || initialAssetWidth),
      minAssetWidth,
      maxAssetWidth,
    )
    const storedInspectorWidth = Number(stored?.inspectorPanelWidth)
    preferredInspectorWidth.value = Number.isFinite(storedInspectorWidth) && storedInspectorWidth > 0
      ? clamp(storedInspectorWidth, minInspectorWidth, maxInspectorWidth)
      : undefined
    panelPriority.value = stored?.panelPriority === 'assets' ? 'assets' : 'inspector'
    syncPanelWidthsToViewport()
    initialized.value = true
  }

  function syncPanelWidthsToViewport() {
    if (disposed) return
    viewportWidth.value = getShellWidth()
  }

  function toggleAssetPanel(expanded?: boolean) {
    const shouldExpand = typeof expanded === 'boolean' ? expanded : !assetPanelVisible.value
    if (shouldExpand && assetPanelVisible.value) return
    if (!shouldExpand && isAssetCollapsed.value) return
    if (shouldExpand) panelPriority.value = 'assets'
    isAssetCollapsed.value = !shouldExpand
    savePanelWidths()
  }

  function toggleInspectorPanel(expanded?: boolean) {
    if (!inspectorVisible.value) return
    const shouldExpand = typeof expanded === 'boolean' ? expanded : !inspectorPanelVisible.value
    if (shouldExpand && inspectorPanelVisible.value) return
    if (!shouldExpand && isInspectorCollapsed.value) return
    if (shouldExpand) panelPriority.value = 'inspector'
    isInspectorCollapsed.value = !shouldExpand
    savePanelWidths()
  }

  function setTrackHeaderWidth(width: number) {
    trackHeaderWidth.value = clampTrackHeaderWidth(width)
    savePanelWidths()
  }

  function setMixerPanelHeight(height: number | null) {
    mixerPanelHeight.value = height === null ? null : clampMixerPanelHeight(height)
    savePanelWidths()
  }

  function toggleMixerPanel(expanded?: boolean) {
    const shouldExpand = typeof expanded === 'boolean' ? expanded : isMixerCollapsed.value
    if (shouldExpand === !isMixerCollapsed.value) return
    isMixerCollapsed.value = !shouldExpand
    savePanelWidths()
  }

  onMounted(() => {
    syncPanelWidthsToViewport()
    void restorePanelWidths()
    window.addEventListener('resize', syncPanelWidthsToViewport)
    if (typeof ResizeObserver !== 'undefined' && shellEl.value) {
      observer = new ResizeObserver(syncPanelWidthsToViewport)
      observer.observe(shellEl.value)
    }
  })

  onBeforeUnmount(() => {
    disposed = true
    stopResize()
    observer?.disconnect()
    window.removeEventListener('resize', syncPanelWidthsToViewport)
    if (saveTimer) {
      clearTimeout(saveTimer)
      saveTimer = null
      persistPanelWidths()
    }
  })

  return {
    viewportWidth,
    inspectorPanelWidth,
    assetPanelWidth,
    isAssetCollapsed,
    isInspectorCollapsed,
    activeResize,
    inspectorVisible,
    inspectorPanelVisible,
    assetPanelVisible,
    assetResizerVisible,
    shellStyle,
    startResize,
    stopResize,
    toggleAssetPanel,
    toggleInspectorPanel,
    mixerPanelVisible: computed(() => !isMixerCollapsed.value),
    trackHeaderWidth,
    setTrackHeaderWidth,
    mixerPanelHeight,
    setMixerPanelHeight,
    toggleMixerPanel,
  }
}
