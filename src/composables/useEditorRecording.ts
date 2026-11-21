import { computed, onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import i18n from '@/i18n'
import { hasEditorPlugins } from '@/utils/editorPlugins'
import { pluginHostCapabilities } from '@/utils/pluginHost'
import type { EditorSession } from '@/types/editor'
import type { useEditorStore } from '@/stores/editor'
import { useEditorPlaybackStore } from '@/stores/editorPlayback'
import { registerWindowCloseGuard } from '@/utils/windowCloseGuards'

type EditorStore = ReturnType<typeof useEditorStore>

type RecordingOptions = {
  editor: EditorStore
  currentTime: Ref<number>
  requestPlayback: (offset?: number) => Promise<boolean>
  pausePlayback: () => void
  followPlayhead?: () => void
}

type RecordingHandle = {
  recordingId: string
}

type RecordingResult = {
  path: string
  name: string
  duration: number
  sampleRate: number
  channels: number
}

type CompletedRecording = RecordingResult & { lockedOverlap: boolean }

type RecordingState = 'idle' | 'preparing' | 'recording' | 'stopping'

class RecordingStartCancelled extends Error {}

const DEVICE_STORAGE_KEY = 'pymss-editor-recording-device'
const PCM_FLUSH_BYTES = 32 * 1024
const MAX_QUEUED_PCM_BYTES = 8 * 1024 * 1024

function bytesToBase64(bytes: Uint8Array) {
  let binary = ''
  const chunkSize = 0x8000
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize))
  }
  return btoa(binary)
}

function floatToPcm16(samples: Float32Array) {
  const bytes = new Uint8Array(samples.length * 2)
  const view = new DataView(bytes.buffer)
  for (let index = 0; index < samples.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, samples[index] || 0))
    const value = sample < 0 ? sample * 0x8000 : sample * 0x7fff
    view.setInt16(index * 2, Math.round(value), true)
  }
  return bytes
}

export function useEditorRecording(options: RecordingOptions) {
  const { editor, currentTime, requestPlayback, pausePlayback, followPlayhead } = options
  const playback = useEditorPlaybackStore()
  const devices = ref<MediaDeviceInfo[]>([])
  const selectedDeviceId = ref(typeof localStorage !== 'undefined' ? localStorage.getItem(DEVICE_STORAGE_KEY) || '' : '')
  const state = ref<RecordingState>('idle')
  const inputLevel = ref(0)
  const elapsed = ref(0)
  const startTime = ref(0)
  const targetTrackId = ref<string | null>(null)
  const error = ref<string | null>(null)
  const isRecording = computed(() => state.value === 'recording')
  const isBusy = computed(() => state.value !== 'idle')

  let mediaStream: MediaStream | null = null
  let audioContext: AudioContext | null = null
  let mediaSource: MediaStreamAudioSourceNode | null = null
  let processor: ScriptProcessorNode | null = null
  let silentGain: GainNode | null = null
  let recordingId = ''
  let recordingProjectId = ''
  let recordingStartedAt = 0
  let recordingSampleRate = 48_000
  let timerRaf: number | null = null
  let pendingChunks: Uint8Array[] = []
  let pendingBytes = 0
  let appendChain = Promise.resolve()
  let appendError: unknown = null
  let lifecycleGeneration = 0
  let allocationPromise: Promise<RecordingHandle> | null = null
  let stopTask: Promise<CompletedRecording | null> | null = null
  let cancelTask: Promise<void> | null = null
  let recordingSession: EditorSession | null = null
  let nativeRecording = false
  let previousLoop = false
  let capturedFrames = 0
  let ownsRecordingTransport = false
  let queuedBytes = 0

  function message(key: string) {
    return String(i18n.global.t(key))
  }

  function recordingFailureMessage(cause: unknown) {
    const errorName = cause && typeof cause === 'object' && 'name' in cause
      ? String((cause as { name?: unknown }).name || '')
      : ''
    if (['NotAllowedError', 'PermissionDeniedError', 'SecurityError'].includes(errorName)) {
      return message('editor.recordingPermissionDenied')
    }
    return cause instanceof Error ? cause.message : String(cause)
  }

  async function refreshDevices(requestPermission = false) {
    if (!navigator.mediaDevices?.enumerateDevices) {
      error.value = message('editor.recordingUnsupported')
      return []
    }
    let permissionStream: MediaStream | null = null
    try {
      if (requestPermission) permissionStream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const available = (await navigator.mediaDevices.enumerateDevices())
        .filter((device) => device.kind === 'audioinput')
      devices.value = available
      if (selectedDeviceId.value && !available.some((device) => device.deviceId === selectedDeviceId.value)) {
        selectDevice('')
      }
      return available
    } catch (cause) {
      if (requestPermission) {
        const detail = recordingFailureMessage(cause)
        error.value = detail
        throw new Error(detail)
      }
      throw cause
    } finally {
      permissionStream?.getTracks().forEach((track) => track.stop())
    }
  }

  function selectDevice(deviceId: string) {
    selectedDeviceId.value = deviceId
    if (typeof localStorage !== 'undefined') {
      if (deviceId) localStorage.setItem(DEVICE_STORAGE_KEY, deviceId)
      else localStorage.removeItem(DEVICE_STORAGE_KEY)
    }
  }

  function mergePendingChunks() {
    const merged = new Uint8Array(pendingBytes)
    let offset = 0
    for (const chunk of pendingChunks) {
      merged.set(chunk, offset)
      offset += chunk.length
    }
    pendingChunks = []
    pendingBytes = 0
    return merged
  }

  function queueFlush() {
    if (!pendingBytes || !recordingId || !recordingProjectId) return
    const payload = mergePendingChunks()
    const projectId = recordingProjectId
    const activeRecordingId = recordingId
    if (queuedBytes + payload.length > MAX_QUEUED_PCM_BYTES) {
      failCapture(new Error(message('editor.recordingWriteBacklog')))
      return
    }
    queuedBytes += payload.length
    appendChain = appendChain.then(async () => {
      try {
        if (appendError) return
        await invoke('append_editor_recording_chunk', {
          projectId,
          recordingId: activeRecordingId,
          dataBase64: bytesToBase64(payload),
        })
      } catch (cause) {
        failCapture(cause)
      } finally {
        queuedBytes -= payload.length
      }
    })
  }

  function failCapture(cause: unknown) {
    appendError ||= new Error(recordingFailureMessage(cause))
    error.value = recordingFailureMessage(appendError)
    if (state.value === 'recording') void stopRecording()
  }

  function updateRecordingTimer() {
    if (state.value !== 'recording') return
    elapsed.value = nativeRecording ? capturedFrames / recordingSampleRate
      : Math.max(0, performance.now() / 1000 - recordingStartedAt)
    playback.setCurrentTime(startTime.value + elapsed.value)
    followPlayhead?.()
    timerRaf = requestAnimationFrame(updateRecordingTimer)
  }

  function stopCaptureGraph() {
    if (timerRaf !== null) cancelAnimationFrame(timerRaf)
    timerRaf = null
    if (processor) processor.onaudioprocess = null
    processor?.disconnect()
    mediaSource?.disconnect()
    silentGain?.disconnect()
    mediaStream?.getTracks().forEach((track) => track.stop())
    mediaStream = null
    mediaSource = null
    processor = null
    silentGain = null
    if (audioContext) void audioContext.close().catch(() => undefined)
    audioContext = null
    inputLevel.value = 0
  }

  async function cleanupPartial(projectId = recordingProjectId, activeRecordingId = recordingId) {
    if (!activeRecordingId || !projectId) return
    await invoke('cancel_editor_recording', {
      projectId,
      recordingId: activeRecordingId,
    }).catch(() => undefined)
  }

  async function discardFinalized(projectId: string, activeRecordingId: string) {
    if (!activeRecordingId || !projectId) return
    await invoke('discard_editor_recording', {
      projectId,
      recordingId: activeRecordingId,
    }).catch(() => undefined)
  }

  function assertStartCurrent(generation: number, projectId: string) {
    if (
      generation !== lifecycleGeneration
      || state.value !== 'preparing'
      || editor.session?.id !== projectId
      || editor.session !== recordingSession
    ) {
      throw new RecordingStartCancelled()
    }
  }

  function releasePreparingResources(stream: MediaStream | null, context: AudioContext | null) {
    stream?.getTracks().forEach((track) => track.stop())
    if (mediaStream === stream) mediaStream = null
    if (audioContext === context) audioContext = null
    if (context && context.state !== 'closed') void context.close().catch(() => undefined)
  }

  function releaseRecordingTransport() {
    if (ownsRecordingTransport && playback.recordingProjectId === recordingProjectId) {
      playback.setRecordingProjectId(null)
      playback.setLoop(previousLoop)
    }
    ownsRecordingTransport = false
    nativeRecording = false
    recordingSession = null
  }

  async function startRecording() {
    if (state.value !== 'idle' || !editor.session) return false
    if (!navigator.mediaDevices?.getUserMedia) {
      error.value = message('editor.recordingUnsupported')
      return false
    }

    state.value = 'preparing'
    error.value = null
    appendError = null
    appendChain = Promise.resolve()
    pendingChunks = []
    pendingBytes = 0
    const generation = ++lifecycleGeneration
    const projectId = editor.session.id
    recordingSession = editor.session
    nativeRecording = hasEditorPlugins(editor.session)
    previousLoop = playback.loop
    capturedFrames = 0
    queuedBytes = 0
    ownsRecordingTransport = false
    let acquiredStream: MediaStream | null = null
    let acquiredContext: AudioContext | null = null
    let allocatedRecordingId = ''
    recordingProjectId = projectId
    startTime.value = Math.max(0, currentTime.value)
    try {
      if (nativeRecording) {
        const capabilities = await pluginHostCapabilities()
        assertStartCurrent(generation, projectId)
        if (!capabilities.enabled || capabilities.recording !== true) {
          throw new Error(message('editor.plugins.recordingBlocked'))
        }
      }
      const constraints: MediaTrackConstraints = {
        channelCount: { ideal: 1 },
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      }
      if (selectedDeviceId.value) constraints.deviceId = { exact: selectedDeviceId.value }
      acquiredStream = await navigator.mediaDevices.getUserMedia({ audio: constraints })
      assertStartCurrent(generation, projectId)
      mediaStream = acquiredStream
      await refreshDevices(false)
      assertStartCurrent(generation, projectId)

      const AudioContextCtor = window.AudioContext
        || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      if (!AudioContextCtor) throw new Error(message('editor.recordingUnsupported'))
      acquiredContext = new AudioContextCtor()
      await acquiredContext.resume()
      assertStartCurrent(generation, projectId)
      audioContext = acquiredContext
      recordingSampleRate = Math.round(acquiredContext.sampleRate)
      const pendingAllocation = invoke<RecordingHandle>('begin_editor_recording', {
        projectId,
      })
      allocationPromise = pendingAllocation
      const handle = await pendingAllocation
      if (allocationPromise === pendingAllocation) allocationPromise = null
      allocatedRecordingId = handle.recordingId
      assertStartCurrent(generation, projectId)
      recordingId = allocatedRecordingId
      const selectedTrack = editor.selectedTrack
      const targetTrack = selectedTrack?.role === 'recording' ? selectedTrack : editor.addRecordingTrack()
      targetTrackId.value = targetTrack.id
      playback.setSuppressedTrackIds([targetTrack.id])

      mediaSource = audioContext.createMediaStreamSource(mediaStream)
      processor = audioContext.createScriptProcessor(4096, 1, 1)
      silentGain = audioContext.createGain()
      silentGain.gain.value = 0
      mediaSource.connect(processor)
      processor.connect(silentGain)
      silentGain.connect(audioContext.destination)
      processor.onaudioprocess = (event) => {
        if (state.value !== 'recording' || generation !== lifecycleGeneration || editor.session !== recordingSession) return
        const input = event.inputBuffer.getChannelData(0)
        let sum = 0
        for (let index = 0; index < input.length; index += 1) sum += input[index] * input[index]
        inputLevel.value = Math.min(1, Math.pow(Math.sqrt(sum / Math.max(1, input.length)) * 3.2, 0.72))
        const pcm = floatToPcm16(input)
        capturedFrames += input.length
        pendingChunks.push(pcm)
        pendingBytes += pcm.length
        if (pendingBytes >= PCM_FLUSH_BYTES) queueFlush()
      }

      if (hasEditorPlugins(editor.session) !== nativeRecording) {
        throw new Error(message('editor.recordingPlaybackChanged'))
      }
      if (nativeRecording) {
        playback.setRecordingProjectId(projectId)
        ownsRecordingTransport = true
        playback.setLoop(false)
        pausePlayback()
        const started = await requestPlayback(startTime.value)
        assertStartCurrent(generation, projectId)
        if (hasEditorPlugins(editor.session) !== nativeRecording) {
          throw new Error(message('editor.recordingPlaybackChanged'))
        }
        if (!started) throw new Error(playback.error || message('editor.recordingPlaybackFailed'))
      }
      state.value = 'recording'
      recordingStartedAt = performance.now() / 1000
      elapsed.value = 0
      timerRaf = requestAnimationFrame(updateRecordingTimer)
      if (!nativeRecording) {
        pausePlayback()
        void requestPlayback(startTime.value)
      }
      return true
    } catch (cause) {
      if (generation === lifecycleGeneration) allocationPromise = null
      const cancelled = cause instanceof RecordingStartCancelled
        || generation !== lifecycleGeneration
        || state.value !== 'preparing'
      if (cancelled) {
        const ownsLifecycle = generation === lifecycleGeneration
        if (ownsLifecycle) {
          stopCaptureGraph()
          if (ownsRecordingTransport) pausePlayback()
        }
        releasePreparingResources(acquiredStream, acquiredContext)
        await cleanupPartial(projectId, allocatedRecordingId)
        if (ownsLifecycle && generation === lifecycleGeneration) {
          lifecycleGeneration += 1
          playback.setSuppressedTrackIds([])
          releaseRecordingTransport()
          state.value = 'idle'
          recordingId = ''
          recordingProjectId = ''
          targetTrackId.value = null
        }
        return false
      }
      stopCaptureGraph()
      if (ownsRecordingTransport) pausePlayback()
      playback.setSuppressedTrackIds([])
      releaseRecordingTransport()
      await cleanupPartial(projectId, allocatedRecordingId || recordingId)
      if (generation !== lifecycleGeneration) return false
      error.value = recordingFailureMessage(cause)
      state.value = 'idle'
      recordingId = ''
      recordingProjectId = ''
      targetTrackId.value = null
      return false
    }
  }

  async function finishRecording() {
    state.value = 'stopping'
    stopCaptureGraph()
    pausePlayback()
    playback.setSuppressedTrackIds([])
    queueFlush()
    await appendChain

    const projectId = recordingProjectId
    const activeRecordingId = recordingId
    let finalized = false
    let attached = false
    try {
      if (appendError) throw appendError
      const targetId = targetTrackId.value
      const targetStillExists = editor.session === recordingSession && editor.session?.id === projectId
        && editor.session.tracks.some((track) => track.id === targetId)
      if (!targetId || !targetStillExists) throw new Error(message('editor.recordingTargetMissing'))
      const result = await invoke<RecordingResult>('finish_editor_recording', {
        projectId,
        recordingId: activeRecordingId,
        sampleRate: recordingSampleRate,
        channels: 1,
      })
      finalized = true
      const targetStillCurrent = editor.session === recordingSession && editor.session?.id === projectId
        && editor.session.tracks.some((track) => track.id === targetId)
      if (!targetStillCurrent) throw new Error(message('editor.recordingTargetMissing'))
      const { lockedOverlap } = await editor.addRecordingClip(targetId, result, startTime.value)
      attached = true
      await editor.flushSave()
      playback.setCurrentTime(startTime.value + result.duration)
      error.value = null
      return { ...result, lockedOverlap }
    } catch (cause) {
      if (finalized && !attached) await discardFinalized(projectId, activeRecordingId)
      else if (!finalized) await cleanupPartial(projectId, activeRecordingId)
      error.value = cause instanceof Error ? cause.message : String(cause)
      return null
    } finally {
      releaseRecordingTransport()
      state.value = 'idle'
      recordingId = ''
      recordingProjectId = ''
      targetTrackId.value = null
    }
  }

  async function stopRecording() {
    if (stopTask) return stopTask
    if (state.value !== 'recording') return null
    const task = finishRecording()
    stopTask = task
    try {
      return await task
    } finally {
      if (stopTask === task) stopTask = null
    }
  }

  async function discardRecording() {
    lifecycleGeneration += 1
    const projectId = recordingProjectId
    const activeRecordingId = recordingId
    const pendingAllocation = allocationPromise
    allocationPromise = null
    state.value = 'stopping'
    stopCaptureGraph()
    if (!nativeRecording || ownsRecordingTransport) pausePlayback()
    pendingChunks = []
    pendingBytes = 0
    const allocatedHandle = pendingAllocation
      ? await pendingAllocation.catch(() => null)
      : null
    await appendChain
    playback.setSuppressedTrackIds([])
    releaseRecordingTransport()
    await cleanupPartial(projectId, activeRecordingId)
    if (allocatedHandle?.recordingId && allocatedHandle.recordingId !== activeRecordingId) {
      await cleanupPartial(projectId, allocatedHandle.recordingId)
    }
    state.value = 'idle'
    recordingId = ''
    recordingProjectId = ''
    targetTrackId.value = null
    elapsed.value = 0
  }

  async function cancelRecording() {
    if (cancelTask) return cancelTask
    if (stopTask) {
      await stopTask
      return
    }
    if (state.value === 'idle') return
    const task = discardRecording()
    cancelTask = task
    try {
      await task
    } finally {
      if (cancelTask === task) cancelTask = null
    }
  }

  async function settleRecording(finalizeActive: boolean) {
    if (stopTask) await stopTask
    else if (finalizeActive && state.value === 'recording') await stopRecording()
    else if (state.value !== 'idle') await cancelRecording()
    if (cancelTask) await cancelTask
  }

  const handleDeviceChange = () => void refreshDevices(false)
  if (typeof navigator !== 'undefined' && navigator.mediaDevices?.addEventListener) {
    navigator.mediaDevices.addEventListener('devicechange', handleDeviceChange)
  }
  if (typeof navigator !== 'undefined') void refreshDevices(false).catch(() => undefined)

  const unregisterWindowCloseGuard = registerWindowCloseGuard(async () => {
    await settleRecording(true)
  }, 100)

  watch(() => editor.session, (nextProject, previousProject) => {
    if (
      previousProject
      && nextProject !== previousProject
      && (state.value === 'recording' || state.value === 'preparing')
    ) {
      void cancelRecording()
    }
  })

  onBeforeUnmount(() => {
    if (typeof navigator !== 'undefined') {
      navigator.mediaDevices?.removeEventListener?.('devicechange', handleDeviceChange)
    }
    if (state.value === 'idle' && !stopTask && !cancelTask) unregisterWindowCloseGuard()
    else void settleRecording(false).finally(unregisterWindowCloseGuard)
  })

  return {
    devices,
    selectedDeviceId,
    state,
    isRecording,
    isBusy,
    inputLevel,
    elapsed,
    startTime,
    targetTrackId,
    error,
    refreshDevices,
    selectDevice,
    startRecording,
    stopRecording,
    cancelRecording,
  }
}
