import { computed, onBeforeUnmount, ref, watch } from 'vue'
import i18n from '@/i18n'
import type { useEditorStore } from '@/stores/editor'
import { useEditorPlaybackStore } from '@/stores/editorPlayback'
import { hasEditorPlugins, pluginProjectSignature } from '@/utils/editorPlugins'
import { ensurePluginSession, PluginHostDialogPendingError, pluginHostCapabilities, pluginHostRequest, type PluginHostStatus } from '@/utils/pluginHost'

type PlaybackHandoff = {
  isCurrent: () => boolean
  takePosition: () => number
}

export function useEditorNativePlayback(editor: ReturnType<typeof useEditorStore>, followPlayhead: () => void) {
  const playback = useEditorPlaybackStore()
  // The native graph can finish the current playback after its last plugin is removed.
  const playingProjectId = ref<string | null>(null)
  const hasPlugins = computed(() => hasEditorPlugins(editor.session))
  const required = computed(() => hasPlugins.value
    || (playingProjectId.value !== null && playingProjectId.value === editor.session?.id
      && (playback.intent === 'play' || playback.status === 'pausing')))
  let activeProjectId: string | null = null
  let failedProjectId: string | null = null
  let generation = 0
  let mixGeneration = 0
  let disposed = false
  let polling = false
  let pollingEnabled = false
  let timer: ReturnType<typeof setTimeout> | null = null
  let releaseTask: Promise<void> | null = null

  function clearPoll() {
    if (timer) clearTimeout(timer)
    timer = null
  }

  function stopPoll() {
    pollingEnabled = false
    clearPoll()
  }

  async function retryAfterDialog<T>(current: () => boolean, operation: () => Promise<T>) {
    while (current()) {
      try { return await operation() }
      catch (cause) {
        if (!(cause instanceof PluginHostDialogPendingError)) throw cause
        if (current()) await new Promise<void>(resolve => setTimeout(resolve, 200))
      }
    }
  }

  function fail(id: number, cause: unknown) {
    if (cause instanceof PluginHostDialogPendingError) {
      if (!disposed && playback.requestId === id && activeProjectId) {
        pollingEnabled = true
        schedulePoll()
      } else if (!disposed && playback.requestId === id) playback.finishPause(id)
      return
    }
    stopPoll()
    if (!disposed && playback.requestId === id) failedProjectId = editor.session?.id || null
    playback.fail(id, cause instanceof Error ? cause.message : String(cause))
  }

  function recordingOwnsPosition(projectId: string) {
    return playback.recordingProjectId === projectId
  }

  async function syncMonitor(projectId: string, current: () => boolean) {
    if (!current()) return false
    const capabilities = await pluginHostCapabilities()
    if (!current()) return false
    const suppressedTrackIds = [...playback.suppressedTrackIds]
    if (capabilities.recording !== true) {
      if (suppressedTrackIds.length) throw new Error(String(i18n.global.t('editor.plugins.recordingBlocked')))
      return true
    }
    await pluginHostRequest({ op: 'monitor', projectId, suppressedTrackIds })
    return current()
  }

  async function ensureLatestProject(project: NonNullable<typeof editor.session>, current: () => boolean) {
    if (releaseTask) await releaseTask
    if (!current()) return
    const canRetry = (signature: string) => current()
      && required.value && signature !== pluginProjectSignature(project)
    let signature: string
    do {
      signature = pluginProjectSignature(project)
      try { await ensurePluginSession(project) }
      catch (cause) {
        if (!canRetry(signature)) throw cause
      }
      // The first load can still be in flight when the chain is edited again.
    } while (canRetry(signature))
    return signature
  }

  function applyStatus(result: PluginHostStatus, id: number, projectId: string, serial: number, preservePosition = false) {
    if (disposed || generation !== serial || playback.requestId !== id || editor.session?.id !== projectId) return
    if (result.projectId && result.projectId !== projectId) return
    if (result.error) { fail(id, new Error(result.error)); return }
    failedProjectId = null
    playback.clearError()
    const recording = recordingOwnsPosition(projectId)
    if (!recording && !preservePosition) playback.setCurrentTime(result.position)
    playback.setMasterLevel(result.masterLevel || [0, 0])
    playback.setTrackLevels(result.levels || {})
    const master = result.masterLevel || [0, 0]
    playback.setLevel((master[0] + master[1]) / 2)
    if (result.playing || (recording && playback.intent === 'play')) playback.finishPlay(id)
    else playback.finishPause(id)
    followPlayhead()
  }

  function schedulePoll() {
    clearPoll()
    if (disposed || !activeProjectId || !pollingEnabled) return
    timer = setTimeout(async () => {
      if (polling || !activeProjectId || disposed) return
      polling = true
      const projectId = activeProjectId
      const serial = generation
      const id = playback.requestId
      const mix = mixGeneration
      try {
        const result = await pluginHostRequest<PluginHostStatus>({ op: 'status', projectId })
        if (mix === mixGeneration && failedProjectId !== projectId) applyStatus(result, id, projectId, serial)
      } catch (cause) {
        if (serial === generation && projectId === activeProjectId && mix === mixGeneration && failedProjectId !== projectId) fail(id, cause)
      } finally {
        polling = false
        if (pollingEnabled && playback.status !== 'error') schedulePoll()
      }
    }, 100)
  }

  async function play(position = playback.currentTime, handoff?: PlaybackHandoff) {
    const project = editor.session
    if (!project || disposed) return false
    const serial = ++generation
    stopPoll()
    const begin = () => {
      playingProjectId.value = project.id
      return playback.beginRequest('play', 'starting')
    }
    let id = handoff ? undefined : begin()
    const current = () => !disposed && serial === generation && editor.session === project
      && (id !== undefined || !handoff || handoff.isCurrent())
    try {
      let preparedSignature: string | undefined
      do {
        preparedSignature = await retryAfterDialog(current, () => ensureLatestProject(project, current))
        if (!current()) return false
        // A direct play request can be paused as soon as the graph is loaded.
        // A handoff leaves transport with Web Audio through monitor setup.
        if (!handoff) activeProjectId = project.id
        if (!await retryAfterDialog(current, () => syncMonitor(project.id, current)) || !current()) return false
      } while (handoff && preparedSignature !== pluginProjectSignature(project))
      if (handoff) position = handoff.takePosition()
      activeProjectId = project.id
      id ??= begin()
      const result = await retryAfterDialog(current, () => pluginHostRequest<PluginHostStatus>({ op: 'play', projectId: project.id, position, loop: recordingOwnsPosition(project.id) ? false : playback.loop }))
      if (!result || !current()) return false
      applyStatus(result, id, project.id, serial)
      pollingEnabled = playback.status !== 'error'
      schedulePoll()
      return playback.status === 'playing'
    } catch (cause) {
      if (!current()) return false
      if (id === undefined) throw cause
      fail(id, cause)
      return false
    }
  }

  async function pause(reset = false) {
    const preservePosition = playback.recordingProjectId !== null
      && playback.recordingProjectId === (activeProjectId || editor.session?.id)
    if (disposed) {
      const id = playback.beginRequest('pause', 'paused')
      if (reset && !preservePosition) playback.setCurrentTime(0)
      playback.finishPause(id)
      return true
    }
    const projectId = activeProjectId
    const serial = ++generation
    const id = playback.beginRequest('pause', 'pausing')
    stopPoll()
    if (!projectId) {
      if (reset && !preservePosition) playback.setCurrentTime(0)
      playback.finishPause(id)
      return true
    }
    const project = editor.session
    const current = () => !disposed && serial === generation && editor.session === project
    try {
      let result = await retryAfterDialog(current, () => pluginHostRequest<PluginHostStatus>({ op: 'pause', projectId }))
      if (reset && result && current()) result = await retryAfterDialog(current, () => pluginHostRequest<PluginHostStatus>({ op: 'seek', projectId, position: 0 }))
      if (!result || !current()) return false
      if (!required.value && editor.session?.id === projectId) {
        const paused = playback.finishPause(id)
        if (paused) playback.clearError()
        return paused
      }
      applyStatus(result, id, projectId, serial, preservePosition)
      if (current() && !preservePosition) {
        pollingEnabled = playback.status !== 'error'
        schedulePoll()
      }
      return playback.requestId === id && playback.status === 'paused'
    } catch (cause) {
      if (!current()) return false
      if (!required.value && editor.session?.id === projectId) {
        const paused = playback.finishPause(id)
        if (paused) playback.clearError()
        return paused
      }
      fail(id, cause)
      return false
    }
  }

  function seek(position: number) {
    const project = editor.session
    if (!project) return
    const playing = playback.intent === 'play'
    if (playing && (playback.status === 'starting' || activeProjectId !== project.id)) {
      playback.setCurrentTime(position)
      void play(position)
      return
    }
    const pausing = playback.status === 'pausing'
    const serial = ++generation
    stopPoll()
    const id = playback.beginRequest(playing ? 'play' : 'pause', playing ? 'starting' : pausing ? 'pausing' : 'paused')
    const current = () => !disposed && serial === generation && editor.session === project
    playback.setCurrentTime(position)
    void (async () => {
      try {
        await retryAfterDialog(current, () => ensureLatestProject(project, current))
        if (!current()) return
        activeProjectId = project.id
        if (!await retryAfterDialog(current, () => syncMonitor(project.id, current)) || !current()) return
        if (pausing) {
          await retryAfterDialog(current, () => pluginHostRequest({ op: 'pause', projectId: project.id }))
          if (!current()) return
        }
        const result = await retryAfterDialog(current, () => pluginHostRequest<PluginHostStatus>({ op: 'seek', projectId: project.id, position }))
        if (!result || !current()) return
        applyStatus(result, id, project.id, serial)
        pollingEnabled = playback.status !== 'error'
        schedulePoll()
      } catch (cause) {
        if (serial === generation && !disposed) fail(id, cause)
      }
    })()
  }

  watch(() => editor.session && pluginProjectSignature(editor.session), async () => {
    const update = ++mixGeneration
    const project = editor.session
    if (!project || (project.id !== activeProjectId && project.id !== failedProjectId) || !required.value) return
    const serial = generation
    const id = playback.requestId
    const current = () => !disposed && update === mixGeneration && serial === generation && editor.session === project && playback.requestId === id
    try {
      await ensurePluginSession(project)
      if (!current()) return
      const recovering = failedProjectId === project.id
      if (recovering) activeProjectId = project.id
      if (!await syncMonitor(project.id, current) || !current()) return
      if (recovering) {
        const result = await pluginHostRequest<PluginHostStatus>({ op: 'status', projectId: project.id })
        if (!current()) return
        applyStatus(result, id, project.id, serial)
        pollingEnabled = playback.status !== 'error'
        schedulePoll()
      }
    } catch (cause) {
      if (!current()) return
      if (!(cause instanceof PluginHostDialogPendingError)) {
        void pluginHostRequest({ op: 'pause', projectId: project.id }).catch(() => undefined)
      }
      fail(id, cause)
    }
  })
  watch(() => playback.suppressedTrackIds, async () => {
    const projectId = activeProjectId
    if (!projectId || editor.session?.id !== projectId || disposed) return
    const serial = generation
    const current = () => !disposed && serial === generation && activeProjectId === projectId && editor.session?.id === projectId
    try { await syncMonitor(projectId, current) }
    catch (cause) { if (current()) fail(playback.requestId, cause) }
  })
  watch(() => playback.loop, loop => {
    if (activeProjectId && playback.intent === 'play' && !recordingOwnsPosition(activeProjectId)) void play(playback.currentTime)
  })
  watch(() => editor.session, (project, oldProject) => {
    if (project?.id === oldProject?.id) return
    generation += 1
    stopPoll()
    playingProjectId.value = null
    activeProjectId = null
    failedProjectId = null
    if (oldProject) void (async () => {
      await editor.releasePluginSession(oldProject).catch(() => undefined)
    })()
  })
  watch([required, hasPlugins], ([needed, plugins], [wasNeeded, hadPlugins]) => {
    if (!plugins && hadPlugins && failedProjectId === editor.session?.id && playback.status === 'error') {
      playback.finishPause(playback.requestId)
      playback.clearError()
    }
    if (!needed && wasNeeded) {
      generation += 1
      stopPoll()
      playingProjectId.value = null
      activeProjectId = null
      failedProjectId = null
      const project = editor.session
      if (project) {
        const task = editor.releasePluginSession(project).catch(() => undefined)
        releaseTask = task
        void task.then(() => { if (releaseTask === task) releaseTask = null })
      }
    }
  })
  onBeforeUnmount(() => {
    disposed = true
    generation += 1
    stopPoll()
    const project = editor.session
    activeProjectId = null
    if (project) void (async () => {
      await pluginHostRequest({ op: 'pause', projectId: project.id }).catch(() => undefined)
      await editor.releasePluginSession(project).catch(() => undefined)
    })()
  })

  return { required, play, pause, seek }
}
