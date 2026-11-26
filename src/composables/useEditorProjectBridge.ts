import { onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { getCurrentWebview } from '@tauri-apps/api/webview'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import type { MessageApiInjection } from 'naive-ui/es/message/src/MessageProvider'
import type { ComposerTranslation } from 'vue-i18n'
import type { useEditorStore } from '@/stores/editor'

type EditorStore = ReturnType<typeof useEditorStore>

type UseEditorProjectBridgeOptions = {
  routeProjectId: Ref<string>
  hasTauriApis: boolean
  editor: EditorStore
  assetPanelEl: Ref<HTMLElement | null>
  mixerRef: Ref<{ containsPoint: (x: number, y: number) => boolean } | null>
  canDropToMixer: () => boolean
  zoomFactor: Ref<number>
  message: MessageApiInjection
  t: ComposerTranslation
}

export function useEditorProjectBridge(options: UseEditorProjectBridgeOptions) {
  const { routeProjectId, hasTauriApis, editor, assetPanelEl, mixerRef, canDropToMixer, zoomFactor, message, t } = options

  const isDraggingExternal = ref(false)
  const isDraggingExternalOnMixer = ref(false)
  const isImportingExternal = ref(false)

  let unlistenDrop: UnlistenFn | null = null
  let unlistenOpenProject: UnlistenFn | null = null
  let disposed = false
  let projectLoadQueue: Promise<void> = Promise.resolve()

  function loadProject(projectId: string) {
    const operation = projectLoadQueue.catch(() => undefined).then(async () => {
      if (disposed || !projectId || editor.session?.id === projectId) return
      if (editor.session) await editor.saveProject()
      if (!disposed) await editor.loadProject(projectId)
    })
    projectLoadQueue = operation
    return operation
  }

  function dropTarget(position: { x: number; y: number } | null | undefined) {
    if (!position || !editor.session || editor.loading || isImportingExternal.value) return null
    // Wry's WKWebView drag events use Cocoa points; WebView2 uses physical pixels.
    const scale = /Mac/i.test(window.navigator.platform) ? zoomFactor.value : window.devicePixelRatio || 1
    const x = position.x / scale
    const y = position.y / scale
    const rect = assetPanelEl.value?.getBoundingClientRect()
    if (rect && rect.width > 0 && rect.height > 0
      && x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) return 'assets'
    if (canDropToMixer() && mixerRef.value?.containsPoint(x, y)) return 'mixer'
    return null
  }

  function clearExternalDrag() {
    isDraggingExternal.value = false
    isDraggingExternalOnMixer.value = false
  }

  async function handleProjectLoad(projectId: string) {
    try {
      await loadProject(projectId)
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      message.error(t('editor.loadFailed', { detail }))
    }
  }

  async function handleExternalDrop(paths: string[], addTracks: boolean) {
    if (!paths.length) return
    const targetSession = editor.session
    isImportingExternal.value = true
    try {
      const result = await editor.scanAssets(paths, { addTracks })
      if (disposed || editor.session !== targetSession) return
      if (result.files.length) message.success(t(addTracks ? 'editor.importTracksSuccess' : 'editor.importSuccess', { count: result.files.length }))
      if (result.warnings?.length) {
        message.warning(t('editor.importWarnings', { detail: result.warnings.slice(0, 3).join('\n') }), { duration: 8000 })
      } else if (!result.files.length) message.warning(t('editor.importEmpty'))
    } catch (error) {
      if (disposed || editor.session !== targetSession) return
      const detail = error instanceof Error ? error.message : String(error)
      message.error(detail || t('editor.importEmpty'))
    } finally {
      isImportingExternal.value = false
    }
  }

  /** Opens the audio file picker and imports the selection as assets or new tracks. */
  async function importAudioFiles(addTracks: boolean) {
    if (!hasTauriApis || !editor.session || editor.loading || isImportingExternal.value) return
    let paths: string[]
    try {
      paths = await invoke<string[]>('pick_audio_files')
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error))
      return
    }
    await handleExternalDrop(paths, addTracks)
  }

  onMounted(async () => {
    if (!hasTauriApis) return

    try {
      const removeDrop = await getCurrentWebview().onDragDropEvent(async (event) => {
        if (disposed) return
        const type = event.payload.type
        if (type === 'over' || type === 'enter') {
          const target = dropTarget(event.payload.position)
          isDraggingExternal.value = target === 'assets'
          isDraggingExternalOnMixer.value = target === 'mixer'
          return
        }

        if (type === 'drop') {
          const target = dropTarget(event.payload.position)
          clearExternalDrag()
          if (!target) return
          const paths = (event.payload as { paths?: string[] }).paths || []
          await handleExternalDrop(paths, target === 'mixer')
          return
        }

        clearExternalDrag()
      })
      if (disposed) removeDrop()
      else unlistenDrop = removeDrop
    } catch {
      // browser preview fallback
    }

    if (disposed) return
    const removeOpenProject = await listen<{ projectId: string }>('pymss://editor-open-project', (event) => {
      if (disposed) return
      void handleProjectLoad(event.payload.projectId)
    })
    if (disposed) removeOpenProject()
    else unlistenOpenProject = removeOpenProject
  })

  watch(routeProjectId, (projectId, previousProjectId) => {
    if (!hasTauriApis) return
    if (!projectId || projectId === previousProjectId) return
    void handleProjectLoad(projectId)
  }, { immediate: true })

  onBeforeUnmount(() => {
    disposed = true
    clearExternalDrag()
    unlistenDrop?.()
    unlistenOpenProject?.()
  })

  return {
    isDraggingExternal,
    isDraggingExternalOnMixer,
    isImportingExternal,
    importAudioFiles,
    loadProject,
  }
}
