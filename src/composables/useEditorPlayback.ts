import { onBeforeUnmount, toValue, type MaybeRefOrGetter, type Ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { convertFileSrc, invoke } from '@tauri-apps/api/core'
import i18n from '@/i18n'
import { normalizeMasterLimiter } from '@/utils/masterLimiter'
import masterLimiterWorkletUrl from '@/audio/masterLimiterProcessor.js?url'
import { clipIsActive, clipMediaTime, clipsForTrack } from '@/utils/editorClips'
import { pluginChainSignature } from '@/utils/editorPlugins'
import { createEditorAudioElement, createPreviewChunkReader, EditorTimelinePlayback, type TimelineAudioClip } from '@/utils/editorAudio'
import type { EditorClip, EditorSource, EditorTrack } from '@/types/editor'
import type { useEditorStore } from '@/stores/editor'
import { useEditorPlaybackStore } from '@/stores/editorPlayback'
import { useEditorNativePlayback } from '@/composables/useEditorNativePlayback'

type EditorStore = ReturnType<typeof useEditorStore>

type PlaybackOptions = {
  editor: EditorStore
  scrollEl: Ref<HTMLElement | null>
  trackHeaderWidth?: MaybeRefOrGetter<number>
}

type ActiveClipEntry = {
  track: EditorTrack
  clip: EditorClip
  source: EditorSource
}

type ManagedAudio = {
  clipId: string
  trackId: string
  sourceId: string
  sourcePath: string
  audio: HTMLAudioElement | null
  metadataReady: boolean
  metadataPromise: Promise<void>
  playPromise: Promise<void> | null
  lastSeekAt: number
  fallbackUrl?: string | null
  graphEnabled?: boolean
  channelMode?: 'mono' | 'stereo'
  sourceNode?: AudioNode | null
  gainNode?: GainNode | null
  balanceSplitter?: ChannelSplitterNode | null
  balanceLeftGain?: GainNode | null
  balanceRightGain?: GainNode | null
  balanceMerger?: ChannelMergerNode | null
  meterSplitter?: ChannelSplitterNode | null
  meterAnalyserLeft?: AnalyserNode | null
  meterAnalyserRight?: AnalyserNode | null
}

type FollowPlayheadMode = 'playback' | 'seek' | 'recording'

const ERROR_SESSION_NOT_LOADED = '请先加载编辑工程'
const ERROR_NO_PLAYABLE_TRACKS = '当前没有可播放的音轨'
const ERROR_NO_LOADED_AUDIO = '没有成功加载任何音频'
const ERROR_AUDIO_CONTEXT = '音频输出尚未就绪，请重新播放'
const ERROR_MISSING_ASSETS = () => String(i18n.global.t('editor.assetOfflineBlocked'))

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value))
}

export function useEditorPlayback(options: PlaybackOptions) {
  const { editor, scrollEl } = options
  // Desktop previews share the FFmpeg downmix and one output clock. Independent
  // media clocks require correction seeks that restart decoding while scrubbing.
  const useScheduledAudio = typeof window !== 'undefined' && /Mac|Win|Linux/i.test(window.navigator?.platform || '')
  const playback = useEditorPlaybackStore()
  const {
    intent,
    status,
    currentTime,
    loop,
    level,
    masterLevel,
    trackLevels,
    error,
    requestId,
    transportVisualState,
    transportPendingAction,
    transportCanToggle,
    isBusy,
    isActuallyPlaying,
  } = storeToRefs(playback)
  const shouldFollowPlayhead = isActuallyPlaying

  const nativePlayback = useEditorNativePlayback(editor, () => followPlayhead())
  const audioEntries = new Map<string, ManagedAudio>()

  let webPlaybackProjectId: string | null = null
  let rafId: number | null = null
  let playbackAnchorTime = 0
  let activeRequestId = 0
  let lastPlaybackTraceAt = 0
  let followScrollRafId: number | null = null
  let followScrollTargetLeft = 0
  let audioContext: AudioContext | null = null
  let disposed = false
  let masterLimiterNode: AudioWorkletNode | null = null
  let masterLimiterSetup: Promise<void> | null = null
  let masterLimiterModuleLoaded = false
  let masterLimiterDrainTimer: ReturnType<typeof setTimeout> | null = null
  let timelinePlayback: EditorTimelinePlayback | null = null
  let masterInputGain: GainNode | null = null
  let masterBalanceSplitter: ChannelSplitterNode | null = null
  let masterBalanceLeftGain: GainNode | null = null
  let masterBalanceRightGain: GainNode | null = null
  let masterBalanceMerger: ChannelMergerNode | null = null
  let masterMeterSplitter: ChannelSplitterNode | null = null
  let masterMeterAnalyserLeft: AnalyserNode | null = null
  let masterMeterAnalyserRight: AnalyserNode | null = null
  const analyserBufferCache = new WeakMap<AnalyserNode, Float32Array<ArrayBuffer>>()
  const audioParamValueCache = new WeakMap<AudioParam, number>()

  function webOwnsPlayback() {
    return webPlaybackProjectId !== null && webPlaybackProjectId === editor.session?.id && intent.value === 'play'
  }

  function usesNativePlayback() {
    return nativePlayback.required.value && !webOwnsPlayback()
  }

  function setAudioParamValue(param: AudioParam, value: number) {
    if (audioParamValueCache.get(param) === value) return
    param.value = value
    audioParamValueCache.set(param, value)
  }

  function resolveAudioUrl(path: string) {
    try {
      return convertFileSrc(path)
    } catch {
      const normalized = path.replace(/\\/g, '/')
      if (/^[a-zA-Z]:\//.test(normalized)) return `file:///${normalized}`
      return path
    }
  }

  function resolveFileFallbackUrl(path: string) {
    const normalized = path.replace(/\\/g, '/')
    if (/^[a-zA-Z]:\//.test(normalized)) return `file:///${normalized}`
    return null
  }

  function ensureAudioContext() {
    if (typeof window === 'undefined') return null
    if (!audioContext) {
      const Ctor = window.AudioContext || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      if (!Ctor) return null
      audioContext = new Ctor()
    }
    if (!masterInputGain) {
      masterInputGain = audioContext.createGain()
      masterBalanceSplitter = audioContext.createChannelSplitter(2)
      masterBalanceLeftGain = audioContext.createGain()
      masterBalanceRightGain = audioContext.createGain()
      masterBalanceMerger = audioContext.createChannelMerger(2)
      masterMeterSplitter = audioContext.createChannelSplitter(2)
      masterMeterAnalyserLeft = audioContext.createAnalyser()
      masterMeterAnalyserRight = audioContext.createAnalyser()

      ;[masterMeterAnalyserLeft, masterMeterAnalyserRight].forEach((node) => {
        node.fftSize = 256
        node.smoothingTimeConstant = 0.78
      })

      masterInputGain.connect(masterBalanceSplitter)
      masterBalanceSplitter.connect(masterBalanceLeftGain, 0)
      masterBalanceSplitter.connect(masterBalanceRightGain, 1)
      masterBalanceLeftGain.connect(masterBalanceMerger, 0, 0)
      masterBalanceRightGain.connect(masterBalanceMerger, 0, 1)
      masterBalanceMerger.connect(masterMeterSplitter)
      masterMeterSplitter.connect(masterMeterAnalyserLeft, 0)
      masterMeterSplitter.connect(masterMeterAnalyserRight, 1)
      masterBalanceMerger.connect(audioContext.destination)
    }
    applyMasterAudioSettings()
    return audioContext
  }

  async function syncMasterLimiter() {
    const config = normalizeMasterLimiter(editor.session?.masterLimiter)
    if (disposed || usesNativePlayback() || !audioContext) return
    if (config.enabled && activeClips().some(({ clip }) => audioEntries.get(clip.id)?.graphEnabled === false)) {
      throw new Error(ERROR_AUDIO_CONTEXT)
    }
    if (masterLimiterNode) {
      masterLimiterNode.port.postMessage({ type: 'configure', config })
      return
    }
    if (!config.enabled) return
    if (masterLimiterSetup) return masterLimiterSetup
    const ctx = audioContext
    masterLimiterSetup = (async () => {
      if (!ctx.audioWorklet || typeof AudioWorkletNode === 'undefined') throw new Error(String(i18n.global.t('editor.limiter.unavailable')))
      if (!masterLimiterModuleLoaded) {
        await ctx.audioWorklet.addModule(masterLimiterWorkletUrl)
        masterLimiterModuleLoaded = true
      }
      if (disposed || audioContext !== ctx || usesNativePlayback()) return
      const latest = normalizeMasterLimiter(editor.session?.masterLimiter)
      if (!latest.enabled) return
      const node = new AudioWorkletNode(ctx, 'pymss-master-limiter', {
        numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [2],
        channelCount: 2, channelCountMode: 'explicit', processorOptions: latest,
      })
      node.onprocessorerror = () => {
        if (disposed || masterLimiterNode !== node) return
        node.onprocessorerror = null
        node.disconnect()
        node.port.close()
        masterLimiterNode = null
        if (audioContext !== ctx) return
        masterBalanceMerger!.disconnect()
        masterBalanceMerger!.connect(masterMeterSplitter!)
        masterBalanceMerger!.connect(ctx.destination)
        // A retained bypass node must not fail dry playback or the native graph.
        if (!usesNativePlayback() && normalizeMasterLimiter(editor.session?.masterLimiter).enabled) {
          failPlayback(activeRequestId, new Error(String(i18n.global.t('editor.limiter.failed'))))
        }
      }
      node.connect(masterMeterSplitter!)
      node.connect(ctx.destination)
      masterBalanceMerger!.disconnect()
      masterBalanceMerger!.connect(node)
      masterLimiterNode = node
    })().catch(cause => {
      // Setup is shared by startup and live edits. Ignore a late failure when
      // the current playback no longer needs this limiter or audio context.
      if (disposed || audioContext !== ctx || usesNativePlayback()
        || !normalizeMasterLimiter(editor.session?.masterLimiter).enabled) return
      throw cause
    }).finally(() => { masterLimiterSetup = null })
    return masterLimiterSetup
  }

  function finishAtEnd() {
    if (intent.value !== 'play' || masterLimiterDrainTimer !== null) return
    const finish = () => {
      masterLimiterDrainTimer = null
      if (disposed || intent.value !== 'play') return
      if (loop.value) void requestPlay(0)
      else requestPause(true)
    }
    const config = normalizeMasterLimiter(editor.session?.masterLimiter)
    const delay = masterLimiterNode && config.enabled && audioContext
      ? Math.ceil(config.attackMs * audioContext.sampleRate / 1000) / audioContext.sampleRate * 1000 : 0
    if (delay > 0) masterLimiterDrainTimer = setTimeout(finish, delay)
    else finish()
  }

  function ensureMeterBuffer(node: AnalyserNode) {
    let cached = analyserBufferCache.get(node)
    if (!cached) {
      cached = new Float32Array(new ArrayBuffer(node.fftSize * Float32Array.BYTES_PER_ELEMENT))
      analyserBufferCache.set(node, cached)
    }
    return cached
  }

  /** Linear sample peak of the analyser's latest window; above 1 when the mix exceeds 0 dBFS. */
  function analyserLevel(node?: AnalyserNode | null) {
    if (!node) return 0
    const buffer = ensureMeterBuffer(node)
    node.getFloatTimeDomainData(buffer)
    let peak = 0
    for (let index = 0; index < buffer.length; index += 1) {
      const sample = Math.abs(buffer[index])
      if (sample > peak) peak = sample
    }
    return peak
  }

  function stereoGainForPan(pan: number) {
    const normalized = clamp(Number(pan || 0), -1, 1)
    return {
      left: normalized <= 0 ? 1 : 1 - normalized,
      right: normalized >= 0 ? 1 : 1 + normalized,
    }
  }

  function applyMasterAudioSettings() {
    if (!masterInputGain || !masterBalanceLeftGain || !masterBalanceRightGain) return
    setAudioParamValue(masterInputGain.gain, clamp(Number(editor.masterVolume || 0), 0, 2))
    const stereo = stereoGainForPan(editor.masterPan)
    setAudioParamValue(masterBalanceLeftGain.gain, stereo.left)
    setAudioParamValue(masterBalanceRightGain.gain, stereo.right)
  }

  function applyTrackAudioSettings(entry: ManagedAudio, track: EditorTrack, baseVolume: number) {
    if (entry.gainNode && entry.balanceLeftGain && entry.balanceRightGain) {
      setAudioParamValue(entry.gainNode.gain, clamp(Number(baseVolume || 0), 0, 2))
      const stereo = stereoGainForPan(track.pan)
      setAudioParamValue(entry.balanceLeftGain.gain, stereo.left)
      setAudioParamValue(entry.balanceRightGain.gain, stereo.right)
      if (entry.audio && entry.audio.volume !== 1) entry.audio.volume = 1
      if (entry.audio?.muted) entry.audio.muted = false
      return
    }

    if (!entry.audio) return
    const muted = baseVolume <= 0.0001
    const volume = clamp(Number(baseVolume || 0), 0, 1)
    if (entry.audio.muted !== muted) entry.audio.muted = muted
    if (entry.audio.volume !== volume) entry.audio.volume = volume
  }

  function channelModeForSource(source: EditorSource): 'mono' | 'stereo' {
    return Number(source.channels || 0) === 1 ? 'mono' : 'stereo'
  }

  function createMetadataPromise(audio: HTMLAudioElement, entry: ManagedAudio) {
    return new Promise<void>((resolve) => {
      if (audio.readyState >= 1) {
        entry.metadataReady = true
        resolve()
        return
      }

      const cleanup = () => {
        audio.removeEventListener('loadedmetadata', onReady)
        audio.removeEventListener('canplay', onReady)
        audio.removeEventListener('error', onDone)
      }

      const onReady = () => {
        cleanup()
        entry.metadataReady = true
        resolve()
      }

      const onDone = () => {
        cleanup()
        resolve()
      }

      audio.addEventListener('loadedmetadata', onReady, { once: true })
      audio.addEventListener('canplay', onReady, { once: true })
      audio.addEventListener('error', onDone, { once: true })
    })
  }

  function releaseEntry(entry: ManagedAudio) {
    pauseEntry(entry)
    entry.sourceNode?.disconnect()
    entry.gainNode?.disconnect()
    entry.balanceSplitter?.disconnect()
    entry.balanceLeftGain?.disconnect()
    entry.balanceRightGain?.disconnect()
    entry.balanceMerger?.disconnect()
    entry.meterSplitter?.disconnect()
    entry.meterAnalyserLeft?.disconnect()
    entry.meterAnalyserRight?.disconnect()
    entry.audio?.removeAttribute('src')
    entry.audio?.load()
  }

  function connectBalanceRouting(entry: ManagedAudio, source: EditorSource) {
    if (!entry.balanceSplitter || !entry.balanceLeftGain || !entry.balanceRightGain) return
    const nextMode = channelModeForSource(source)
    if (entry.channelMode === nextMode) return

    entry.balanceSplitter.disconnect()
    if (nextMode === 'mono') {
      entry.balanceSplitter.connect(entry.balanceLeftGain, 0)
      entry.balanceSplitter.connect(entry.balanceRightGain, 0)
    } else {
      entry.balanceSplitter.connect(entry.balanceLeftGain, 0)
      entry.balanceSplitter.connect(entry.balanceRightGain, 1)
    }
    entry.channelMode = nextMode
  }

  function connectEntryAudioGraph(entry: ManagedAudio, source: EditorSource) {
    const ctx = ensureAudioContext()
    if (!ctx || !masterInputGain) return
    if (entry.graphEnabled === false) return
    if (entry.sourceNode) {
      connectBalanceRouting(entry, source)
      return
    }

    try {
      entry.sourceNode = entry.audio ? ctx.createMediaElementSource(entry.audio) : ctx.createGain()
      entry.gainNode = ctx.createGain()
      entry.balanceSplitter = ctx.createChannelSplitter(2)
      entry.balanceLeftGain = ctx.createGain()
      entry.balanceRightGain = ctx.createGain()
      entry.balanceMerger = ctx.createChannelMerger(2)
      entry.meterSplitter = ctx.createChannelSplitter(2)
      entry.meterAnalyserLeft = ctx.createAnalyser()
      entry.meterAnalyserRight = ctx.createAnalyser()

      ;[entry.meterAnalyserLeft, entry.meterAnalyserRight].forEach((node) => {
        node.fftSize = 256
        node.smoothingTimeConstant = 0.76
      })

      entry.sourceNode.connect(entry.gainNode)
      entry.gainNode.connect(entry.balanceSplitter)
      connectBalanceRouting(entry, source)

      entry.balanceLeftGain.connect(entry.balanceMerger, 0, 0)
      entry.balanceRightGain.connect(entry.balanceMerger, 0, 1)
      entry.balanceMerger.connect(entry.meterSplitter)
      entry.meterSplitter.connect(entry.meterAnalyserLeft, 0)
      entry.meterSplitter.connect(entry.meterAnalyserRight, 1)
      entry.balanceMerger.connect(masterInputGain)
      entry.graphEnabled = true
    } catch {
      entry.graphEnabled = false
      entry.sourceNode = null
      entry.gainNode = null
      entry.balanceSplitter = null
      entry.balanceLeftGain = null
      entry.balanceRightGain = null
      entry.balanceMerger = null
      entry.meterSplitter = null
      entry.meterAnalyserLeft = null
      entry.meterAnalyserRight = null
    }
  }

  function computeDuration() {
    return Math.max(0, editor.duration || 0)
  }

  function clampTime(time: number, duration = computeDuration()) {
    return Math.max(0, Math.min(time, duration))
  }

  function activeClips() {
    const session = editor.session
    if (!session) return []
    const hasSolo = session.tracks.some((track) => track.solo)
    const suppressedTrackIds = new Set(playback.suppressedTrackIds)
    const entries: ActiveClipEntry[] = []
    for (const track of session.tracks.filter((item) => (
      !item.muted && !suppressedTrackIds.has(item.id) && (!hasSolo || item.solo)
    ))) {
      for (const clip of clipsForTrack(track, editor.sourceMap)) {
        if (clip.muted || clip.duration <= 0) continue
        const source = editor.sourceMap.get(clip.assetId)
        if (source && !source.missing) entries.push({ track, clip, source })
      }
    }
    return entries
  }

  function hasMissingSourcesInUse() {
    // Silent tracks and clips are skipped, as export and the native host skip them.
    return editor.missingSourcesInUse({ suppressedTrackIds: playback.suppressedTrackIds, skipZeroGain: true }).length > 0
  }

  function trackSignature() {
    return activeClips()
      .map(({ track, clip, source }) => [
        track.id,
        clip.id,
        source.id,
        source.path,
        useScheduledAudio ? source.sampleRate : null,
        useScheduledAudio ? source.duration : null,
        source.channels,
        track.muted,
        track.solo,
        clip.start,
        clip.offset,
        clip.duration,
        clip.muted,
      ].join(':'))
      .join('|')
  }

  function trackMixSignature() {
    return activeClips()
      .map(({ track, clip }) => [
        track.id,
        clip.id,
        track.volume,
        track.pan,
        clip.volume,
        clip.fadeIn,
        clip.fadeOut,
      ].join(':'))
      .join('|')
  }

  function ensureAudioEntry(track: EditorTrack, clip: EditorClip, source: EditorSource) {
    const cached = audioEntries.get(clip.id)
    if (cached && cached.sourceId === source.id && cached.sourcePath === source.path) {
      connectEntryAudioGraph(cached, source)
      return cached
    }

    if (cached) {
      releaseEntry(cached)
      audioEntries.delete(clip.id)
    }

    const primaryUrl = resolveAudioUrl(source.path)
    const fallbackUrl = resolveFileFallbackUrl(source.path)
    const ctx = ensureAudioContext()
    const isScheduled = Boolean(ctx && useScheduledAudio)
    // Scheduled audio connects directly to the graph; only the media transport
    // owns an HTMLAudioElement.
    const audio = isScheduled ? null : createEditorAudioElement(primaryUrl)

    const entry: ManagedAudio = {
      clipId: clip.id,
      trackId: track.id,
      sourceId: source.id,
      sourcePath: source.path,
      audio,
      fallbackUrl: fallbackUrl && fallbackUrl !== primaryUrl ? fallbackUrl : null,
      graphEnabled: undefined,
      metadataReady: !audio || audio.readyState >= 1,
      metadataPromise: Promise.resolve(),
      playPromise: null,
      lastSeekAt: -Infinity,
    }

    audio?.addEventListener('error', () => {
      if (audioEntries.get(entry.clipId) !== entry) return
      if (entry.fallbackUrl && audio.currentSrc !== entry.fallbackUrl && audio.src !== entry.fallbackUrl) {
        entry.metadataReady = false
        audio.src = entry.fallbackUrl
        audio.load()
        return
      }
      if (intent.value === 'play' && activeClips().some(({ clip }) => (
        clip.id === entry.clipId && clipIsActive(clip, currentTime.value)
      ))) {
        failPlayback(activeRequestId, new Error(audio.error?.message || ERROR_NO_LOADED_AUDIO))
      }
    })

    entry.metadataPromise = audio ? createMetadataPromise(audio, entry) : Promise.resolve()
    audioEntries.set(clip.id, entry)
    connectEntryAudioGraph(entry, source)
    audio?.load()
    return entry
  }

  function timelineEntries(): TimelineAudioClip[] {
    return activeClips().flatMap(({ track, clip, source }) => {
      const entry = ensureAudioEntry(track, clip, source)
      return !entry.audio && entry.sourceNode ? [{ clip, source, output: entry.sourceNode }] : []
    })
  }

  function ensureTimelinePlayback() {
    const ctx = ensureAudioContext()
    if (!ctx || !useScheduledAudio) return null
    if (!timelinePlayback) {
      timelinePlayback = new EditorTimelinePlayback(ctx, createPreviewChunkReader(ctx),
        cause => failPlayback(activeRequestId, cause), waiting => {
          if (intent.value !== 'play') return
          if (waiting) {
            playback.setCurrentTime(timelinePlayback?.currentTime ?? currentTime.value)
            playback.setStatus('starting')
          }
          else {
            playback.finishPlay(activeRequestId)
            stopRaf()
            rafId = requestAnimationFrame(tick)
          }
        }, () => {
          finishAtEnd()
        })
    }
    return timelinePlayback
  }

  function setAudioTime(entry: ManagedAudio, time: number) {
    const audio = entry.audio
    if (!audio) return
    const maxTime = Number.isFinite(audio.duration) && audio.duration > 0
      ? Math.max(0, audio.duration - 0.01)
      : time
    const next = Math.max(0, Math.min(time, maxTime))
    if (Math.abs(audio.currentTime - next) < 0.001) return
    try {
      audio.currentTime = next
      entry.lastSeekAt = performance.now()
    } catch {
      // ignore early metadata timing errors
    }
  }

  function computeClipVolume(track: EditorTrack, clip: EditorClip, time: number) {
    if (!clipIsActive(clip, time) || clip.muted) return 0
    let gain = Math.max(0, track.volume) * Math.max(0, clip.volume)
    const relativeTime = Math.max(0, time - clip.start)
    const duration = Math.max(0, Number(clip.duration || 0))
    const fadeIn = Math.max(0, Number(clip.fadeIn || 0))
    const fadeOut = Math.max(0, Number(clip.fadeOut || 0))

    if (fadeIn > 0 && relativeTime < fadeIn) {
      gain *= Math.max(0, Math.min(1, relativeTime / fadeIn))
    }

    if (fadeOut > 0 && duration > 0) {
      const fadeOutStart = Math.max(0, duration - fadeOut)
      if (relativeTime > fadeOutStart) {
        gain *= Math.max(0, Math.min(1, (duration - relativeTime) / fadeOut))
      }
    }

    return Math.max(0, Math.min(2, gain))
  }

  function pauseInactiveAudios(activeIds: Set<string>) {
    const staleIds: string[] = []
    audioEntries.forEach((entry, clipId) => {
      if (activeIds.has(clipId)) return
      releaseEntry(entry)
      staleIds.push(clipId)
    })
    staleIds.forEach((clipId) => audioEntries.delete(clipId))
  }

  function applyClipStates(time = currentTime.value, playNow = status.value === 'playing') {
    const entries = activeClips()
    const knownIds = new Set(entries.map(({ clip }) => clip.id))
    const audibleTrackIds = new Set(entries.map(({ track }) => track.id))
    if (ensureTimelinePlayback()) {
      for (const { track, clip, source } of entries) {
        const entry = ensureAudioEntry(track, clip, source)
        // Clip volume/fades are sample-timed by the scheduler; this is track gain.
        applyTrackAudioSettings(entry, track, track.volume)
      }
      pauseInactiveAudios(knownIds)
      playback.clearTrackLevels([...audibleTrackIds])
      return
    }
    const waiting = playNow && (
      (audioContext && audioContext.state !== 'running')
      || entries.some(({ clip }) => {
        if (!clipIsActive(clip, time)) return false
        const audio = audioEntries.get(clip.id)?.audio
        return audio && !audio.ended && (audio.seeking || audio.readyState < 3)
      })
    )

    // Hold the whole mix while a decoder seeks/buffers. Seeking on every frame
    // restarts slow WebKit seeks and can prevent the audio from ever catching up.
    if (waiting) pauseAllAudios()

    for (const { track, clip, source } of entries) {
      const entry = ensureAudioEntry(track, clip, source)
      if (!entry.audio) continue
      const active = clipIsActive(clip, time)
      const volume = computeClipVolume(track, clip, time)
      applyTrackAudioSettings(entry, track, volume)
      if (!active) {
        if (!entry.audio.paused) pauseEntry(entry)
        continue
      }
      if (playNow && entry.audio.error) {
        failPlayback(activeRequestId, new Error(entry.audio.error.message || ERROR_NO_LOADED_AUDIO))
        return
      }
      // A completed source may be slightly ahead of the slowest track. Calling
      // play() here would restart it at zero while the mix catches up.
      if (playNow && entry.audio.ended) continue

      const mediaTime = clipMediaTime(clip, time)
      if (!waiting && !entry.audio.seeking && entry.audio.readyState >= 3
        && performance.now() - entry.lastSeekAt >= 1000
        && Math.abs(entry.audio.currentTime - mediaTime) > 0.16) {
        setAudioTime(entry, mediaTime)
      }
      if (playNow && !waiting && entry.audio.paused) {
        const id = activeRequestId
        void playEntry(entry).catch((cause) => {
          failPlayback(id, cause)
        })
      } else if (!playNow && !entry.audio.paused) {
        pauseEntry(entry)
      }
    }

    pauseInactiveAudios(knownIds)
    playback.clearTrackLevels([...audibleTrackIds])
  }

  async function preloadActiveTracks() {
    if (disposed || usesNativePlayback()) return
    const projectId = editor.session?.id
    const entries = activeClips().map(({ track, clip, source }) => ensureAudioEntry(track, clip, source))
    await Promise.allSettled(entries.map((entry) => entry.metadataPromise))
    if (disposed || projectId !== editor.session?.id || usesNativePlayback()) return
    const timeline = ensureTimelinePlayback()
    if (timeline) {
      // Warm just the current window; a project does not wait for full decode.
      await timeline.preload(timelineEntries(), currentTime.value, computeDuration()).catch(() => undefined)
    }
  }

  function animateFollowScroll(targetLeft: number) {
    const el = scrollEl.value
    if (!el) return

    followScrollTargetLeft = targetLeft

    if (Math.abs(el.scrollLeft - targetLeft) < 1) {
      el.scrollLeft = targetLeft
      stopFollowScroll()
      return
    }

    if (followScrollRafId !== null) return

    const step = () => {
      const host = scrollEl.value
      if (!host) {
        stopFollowScroll()
        return
      }

      const delta = followScrollTargetLeft - host.scrollLeft
      if (Math.abs(delta) < 1) {
        host.scrollLeft = followScrollTargetLeft
        stopFollowScroll()
        return
      }

      host.scrollLeft += delta * 0.2
      followScrollRafId = requestAnimationFrame(step)
    }

    followScrollRafId = requestAnimationFrame(step)
  }

  function followPlayhead(mode: FollowPlayheadMode = 'playback') {
    if (mode === 'playback' && intent.value !== 'play') return
    const el = scrollEl.value
    if (!el) return
    const trackHeaderWidth = toValue(options.trackHeaderWidth ?? 0)
    const x = trackHeaderWidth + currentTime.value * editor.pixelsPerSecond
    const maxScrollLeft = Math.max(0, el.scrollWidth - el.clientWidth)
    // The timeline can grow while recording. Clamp to the rendered scroll
    // content instead of the project duration captured before recording began.
    const clampedX = Math.max(trackHeaderWidth, Math.min(el.scrollWidth, x))

    if (mode === 'seek') {
      const targetLeft = Math.max(0, Math.min(maxScrollLeft, clampedX - el.clientWidth * 0.5))
      animateFollowScroll(targetLeft)
      return
    }

    const viewportLeft = el.scrollLeft
    const viewportRight = viewportLeft + el.clientWidth
    const leftGuard = viewportLeft + Math.min(48, el.clientWidth * 0.08)
    const rightGuard = viewportRight - Math.max(56, el.clientWidth * 0.08)

    if (clampedX < leftGuard) {
      const targetLeft = Math.max(0, Math.min(maxScrollLeft, clampedX - el.clientWidth * 0.2))
      animateFollowScroll(targetLeft)
      return
    }

    if (clampedX > rightGuard) {
      const pageAdvance = Math.max(el.clientWidth * 0.82, 240)
      const targetLeft = Math.max(
        0,
        Math.min(maxScrollLeft, Math.max(viewportLeft + pageAdvance, clampedX - el.clientWidth * 0.18)),
      )
      animateFollowScroll(targetLeft)
    }
  }

  function stopFollowScroll() {
    if (followScrollRafId !== null) {
      cancelAnimationFrame(followScrollRafId)
      followScrollRafId = null
    }
  }

  watch(intent, next => {
    if (next === 'pause') stopFollowScroll()
  }, { flush: 'sync' })

  function stopRaf() {
    if (rafId !== null) {
      cancelAnimationFrame(rafId)
      rafId = null
    }
    stopFollowScroll()
  }

  function pauseEntry(entry: ManagedAudio) {
    // Invalidate this operation before pause() rejects its pending play().
    entry.playPromise = null
    entry.audio?.pause()
  }

  function pauseAllAudios(keepPending = false) {
    if (masterLimiterDrainTimer !== null) {
      clearTimeout(masterLimiterDrainTimer)
      masterLimiterDrainTimer = null
    }
    timelinePlayback?.pause(keepPending)
    audioEntries.forEach(pauseEntry)
    masterLimiterNode?.port.postMessage({ type: 'reset' })
  }

  function syncPauseAt(time: number) {
    pauseAllAudios()
    for (const { track, clip, source } of activeClips()) {
      const entry = ensureAudioEntry(track, clip, source)
      setAudioTime(entry, clipMediaTime(clip, time))
    }
  }

  function sampleCurrentTime() {
    if (status.value !== 'playing') return currentTime.value
    if (timelinePlayback) return timelinePlayback.currentTime
    const time = currentTime.value
    const entries = activeClips()
    const active = entries.filter(({ clip }) => clipIsActive(clip, time))
    let next = performance.now() / 1000 - playbackAnchorTime
    if (audioContext && audioContext.state !== 'running') {
      next = time
    } else if (active.length) {
      // Use actual media progress, including clip offsets, instead of making
      // stalled decoders chase a wall clock. The slowest track paces the mix.
      next = Math.min(...active.map(({ clip }) => {
        const audio = audioEntries.get(clip.id)?.audio
        if (!audio) return time
        if (audio.ended) return clip.start + clip.duration
        if (audio.paused || audio.seeking || audio.readyState < 3) return time
        return Math.min(clip.start + clip.duration, clip.start + audio.currentTime - clip.offset)
      }))
    }
    // Wall time is only used across gaps. Do not skip the next clip when a
    // frame is delayed, or start it halfway through its media.
    for (const { clip } of entries) {
      if (clip.start > time) next = Math.min(next, clip.start)
    }
    next = Math.max(time, next)
    playbackAnchorTime = performance.now() / 1000 - next
    return next
  }

  function playEntry(entry: ManagedAudio) {
    if (!entry.audio) return Promise.reject(new Error(ERROR_AUDIO_CONTEXT))
    if (entry.playPromise) return entry.playPromise
    const pending = entry.audio.play().catch((cause) => {
      // Only a play operation explicitly cancelled by pauseEntry is ignored.
      if (entry.playPromise === pending) throw cause
    }).finally(() => {
      if (entry.playPromise === pending) entry.playPromise = null
    })
    entry.playPromise = pending
    return pending
  }

  function failPlayback(id: number, cause: unknown) {
    if (id !== activeRequestId) return
    webPlaybackProjectId = null
    stopRaf()
    playback.fail(id, cause instanceof Error ? cause.message : String(cause))
    // Failure also cancels pending resume/play/metadata work. Their subsequent
    // AbortErrors must not replace the original decoder or output error.
    activeRequestId = playback.nextRequest()
    pauseAllAudios()
    tracePlayback('error')
  }

  function tracePlayback(event: string) {
    if (!(window as typeof window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__) return
    const timelineTime = timelinePlayback?.currentTime ?? currentTime.value
    const clips = new Map(activeClips().map(({ clip }) => [clip.id, clip]))
    void invoke('debug_editor_playback', { snapshot: {
      event, status: status.value, time: currentTime.value, error: error.value,
      context: audioContext ? {
        state: audioContext.state, time: audioContext.currentTime, sampleRate: audioContext.sampleRate,
      } : null,
      bufferedUntil: timelinePlayback?.bufferedUntil,
      tracks: [...audioEntries.values()].map(({ clipId, audio }) => {
        const clip = clips.get(clipId)
        const active = Boolean(clip && clipIsActive(clip, timelineTime))
        return {
          clipId, backend: audio ? 'media' : 'scheduled-chunks', active,
          mediaTime: audio?.currentTime,
          scheduledMediaTime: !audio && active && clip ? clipMediaTime(clip, timelineTime) : undefined,
          paused: audio ? audio.paused : !timelinePlayback?.isPlaying || !active,
          seeking: audio?.seeking, readyState: audio?.readyState, error: audio?.error?.message,
        }
      }),
    } }).catch(() => undefined)
  }

  function updateLevelMeter() {
    if (status.value !== 'playing') {
      playback.setLevel(0)
      playback.setMasterLevel([0, 0])
      return
    }

    const nextLevels: Record<string, [number, number]> = {}
    for (const { track, clip, source } of activeClips()) {
      if (!clipIsActive(clip, currentTime.value)) continue
      const entry = ensureAudioEntry(track, clip, source)
      const left = analyserLevel(entry.meterAnalyserLeft)
      const right = analyserLevel(entry.meterAnalyserRight)
      const current = nextLevels[track.id] || [0, 0]
      nextLevels[track.id] = [Math.max(current[0], left), Math.max(current[1], right)]
    }
    playback.setTrackLevels(nextLevels)

    const masterStereo: [number, number] = [
      analyserLevel(masterMeterAnalyserLeft),
      analyserLevel(masterMeterAnalyserRight),
    ]
    playback.setMasterLevel(masterStereo)
    playback.setLevel((masterStereo[0] + masterStereo[1]) / 2)
  }

  function releaseAudios() {
    webPlaybackProjectId = null
    timelinePlayback?.dispose()
    timelinePlayback = null
    pauseAllAudios()
    audioEntries.forEach((entry) => {
      releaseEntry(entry)
    })
    audioEntries.clear()
    playback.setMasterLevel([0, 0])
    playback.clearTrackLevels()
  }

  function finishPaused(id: number, time: number) {
    stopRaf()
    syncPauseAt(time)
    playback.setCurrentTime(clampTime(time))
    playback.setLevel(0)
    playback.setMasterLevel([0, 0])
    playback.clearTrackLevels()
    playback.finishPause(id)
  }

  function syncPausedAudiosToTime(time: number) {
    const id = activeRequestId
    const nextTime = clampTime(time)
    playback.setCurrentTime(nextTime)
    playbackAnchorTime = performance.now() / 1000 - nextTime
    applyClipStates(nextTime, false)

    const entries = activeClips()
    for (const { track, clip, source } of entries) {
      const entry = ensureAudioEntry(track, clip, source)
      const applySeek = () => setAudioTime(entry, clipMediaTime(clip, nextTime))
      if (entry.metadataReady) applySeek()
      else void entry.metadataPromise.then(() => {
        if (id !== activeRequestId || audioEntries.get(clip.id) !== entry) return
        applySeek()
      })
    }
  }

  async function syncActiveAudios(targetTime: number, expectedRequestId: number) {
    if (hasMissingSourcesInUse()) throw new Error(ERROR_MISSING_ASSETS())
    // The scheduler will retain reads needed at the new position and cancel
    // the rest once it has computed that window.
    pauseAllAudios(true)
    const entries = activeClips()
    if (!computeDuration()) {
      timelinePlayback?.pause()
      pauseInactiveAudios(new Set())
      return false
    }

    const ctx = ensureAudioContext()
    if (ctx && ctx.state !== 'running') {
      await ctx.resume()
    }
    if (expectedRequestId !== activeRequestId) return false
    if (ctx && ctx.state !== 'running') throw new Error(ERROR_AUDIO_CONTEXT)
    if (normalizeMasterLimiter(editor.session?.masterLimiter).enabled && !ctx) throw new Error(ERROR_AUDIO_CONTEXT)
    await syncMasterLimiter()
    if (expectedRequestId !== activeRequestId) return false

    const timeline = ensureTimelinePlayback()
    if (timeline) {
      const duration = computeDuration()
      const nextTime = clampTime(targetTime, duration)
      pauseInactiveAudios(new Set(entries.map(({ clip }) => clip.id)))
      playback.setCurrentTime(nextTime)
      applyClipStates(nextTime, false)
      const scheduledEntries = timelineEntries()
      if (scheduledEntries.length !== entries.length) throw new Error(ERROR_AUDIO_CONTEXT)
      const started = await timeline.start(scheduledEntries, nextTime, duration)
      return expectedRequestId === activeRequestId && started
    }

    const activeIds = new Set(entries.map(({ clip }) => clip.id))
    pauseInactiveAudios(activeIds)

    const managed = entries.map(({ track, clip, source }) => ({
      clip,
      entry: ensureAudioEntry(track, clip, source),
    }))

    if (normalizeMasterLimiter(editor.session?.masterLimiter).enabled && managed.some(({ entry }) => !entry.graphEnabled)) {
      throw new Error(ERROR_AUDIO_CONTEXT)
    }
    const nextTime = clampTime(targetTime)
    playback.setCurrentTime(nextTime)
    playbackAnchorTime = performance.now() / 1000 - nextTime

    for (const { entry, clip } of managed) {
      setAudioTime(entry, clipMediaTime(clip, nextTime))
      if (!entry.metadataReady) {
        void entry.metadataPromise.then(() => {
          if (expectedRequestId !== activeRequestId || audioEntries.get(clip.id) !== entry) return
          setAudioTime(entry, clipMediaTime(clip, nextTime))
        })
      }
    }

    applyClipStates(nextTime, false)

    const activeManaged = managed.filter(({ clip }) => clipIsActive(clip, nextTime))
    await Promise.all(activeManaged.map(({ entry }) => playEntry(entry)))
    if (expectedRequestId !== activeRequestId) return false
    playbackAnchorTime = performance.now() / 1000 - nextTime
    return true
  }

  async function requestPlay(offset = currentTime.value) {
    if (usesNativePlayback()) {
      activeRequestId = playback.nextRequest()
      stopRaf()
      releaseAudios()
      return nativePlayback.play(offset)
    }
    playback.clearError()

    if (!editor.session) {
      return rejectPlay(ERROR_SESSION_NOT_LOADED)
    }

    if (hasMissingSourcesInUse()) {
      return rejectPlay(ERROR_MISSING_ASSETS())
    }

    if (!computeDuration()) {
      return rejectPlay(ERROR_NO_PLAYABLE_TRACKS)
    }

    webPlaybackProjectId = editor.session.id
    const id = playback.beginRequest('play', 'starting')
    activeRequestId = id

    const started = await syncActiveAudios(offset, id).catch((cause) => {
      if (id === activeRequestId) failPlayback(id, cause)
      return false
    })

    if (id !== activeRequestId) return false
    if (intent.value !== 'play') return false

    if (!started) {
      if (!error.value) failPlayback(id, ERROR_NO_LOADED_AUDIO)
      return false
    }

    if (!playback.finishPlay(id)) return false
    playbackAnchorTime = performance.now() / 1000 - currentTime.value
    followPlayhead('seek')
    stopRaf()
    rafId = requestAnimationFrame(tick)
    return true
  }

  function rejectPlay(message: string) {
    const id = playback.beginRequest('pause', 'paused')
    activeRequestId = id
    // Rejecting a replacement request must also stop an existing transport and
    // invalidate its outstanding decodes, not only change the button state.
    failPlayback(id, new Error(message))
    return false
  }

  function requestPause(reset = false) {
    if (usesNativePlayback()) return nativePlayback.pause(reset)
    const nextTime = reset ? 0 : clampTime(sampleCurrentTime())
    webPlaybackProjectId = null
    const id = playback.beginRequest('pause', 'pausing')
    activeRequestId = id
    finishPaused(id, nextTime)
    return true
  }

  function toggleTransport() {
    if (intent.value === 'play') {
      requestPause(false)
      return Promise.resolve(true)
    }
    return requestPlay(currentTime.value)
  }

  function stop(reset = true) {
    requestPause(reset)
  }

  function tick() {
    if (intent.value !== 'play' || status.value !== 'playing') {
      stopRaf()
      return
    }

    const time = clampTime(sampleCurrentTime())
    playback.setCurrentTime(time)
    applyClipStates(time, true)
    if (intent.value !== 'play' || status.value !== 'playing') return
    updateLevelMeter()
    if (performance.now() - lastPlaybackTraceAt >= 2000) {
      lastPlaybackTraceAt = performance.now()
      tracePlayback('playing')
    }
    followPlayhead()

    const total = computeDuration()
    if (total > 0 && time >= total - (timelinePlayback || normalizeMasterLimiter(editor.session?.masterLimiter).enabled ? 0 : 0.04)) {
      finishAtEnd()
      return
    }

    rafId = requestAnimationFrame(tick)
  }

  function seek(time: number) {
    const next = clampTime(time)
    if (usesNativePlayback()) return nativePlayback.seek(next)
    const playing = intent.value === 'play'
    const id = playback.beginRequest(playing ? 'play' : 'pause', playing ? 'starting' : 'paused')
    activeRequestId = id
    stopRaf()
    syncPausedAudiosToTime(next)

    if (playing) {
      followPlayhead('seek')
      void syncActiveAudios(next, id).then((started) => {
        if (id !== activeRequestId || intent.value !== 'play') return
        if (!started) {
          failPlayback(id, ERROR_NO_LOADED_AUDIO)
          return
        }
        if (!playback.finishPlay(id)) return
        stopRaf()
        rafId = requestAnimationFrame(tick)
      }).catch((cause) => {
        failPlayback(id, cause)
      })
    }
  }

  function applyMasterVolume() {
    if (usesNativePlayback()) return
    applyMasterAudioSettings()
    for (const { track, clip, source } of activeClips()) {
      const entry = ensureAudioEntry(track, clip, source)
      applyTrackAudioSettings(entry, track, timelinePlayback ? track.volume : computeClipVolume(track, clip, currentTime.value))
    }
    timelinePlayback?.updateMix(timelineEntries())
  }

  watch(() => editor.masterVolume, () => {
    applyMasterVolume()
  })

  watch(() => editor.session?.masterLimiter, () => {
    const id = activeRequestId
    void syncMasterLimiter().catch(cause => failPlayback(id, cause))
  }, { deep: true })

  watch(trackMixSignature, applyMasterVolume)

  watch(() => editor.masterPan, () => {
    if (usesNativePlayback()) return
    applyMasterAudioSettings()
  })

  watch(
    () => editor.session?.id || '',
    () => {
      stop(true)
      releaseAudios()
      playback.clearError()
    },
  )

  watch(
    [trackSignature, () => editor.duration, hasMissingSourcesInUse],
    ([signature, duration, missing], [previousSignature, , previouslyMissing]) => {
      if (usesNativePlayback()) return
      if (intent.value === 'play') {
        if (!duration) {
          requestPause(true)
          return
        }
        if (signature === previousSignature && missing === previouslyMissing) {
          timelinePlayback?.setDuration(duration)
          return
        }
        const id = playback.beginRequest('play', 'starting')
        activeRequestId = id
        void syncActiveAudios(currentTime.value, id).then((started) => {
          if (id !== activeRequestId || intent.value !== 'play') return
          if (!started) {
            failPlayback(id, ERROR_NO_LOADED_AUDIO)
            return
          }
          if (!playback.finishPlay(id)) return
          stopRaf()
          rafId = requestAnimationFrame(tick)
        }).catch((cause) => {
          failPlayback(id, cause)
        })
      } else {
        syncPausedAudiosToTime(currentTime.value)
        void preloadActiveTracks()
      }
    },
    { immediate: true },
  )

  function pendingPluginChain() {
    const project = editor.session
    if (!project || !nativePlayback.required.value || !webOwnsPlayback()) return ''
    return pluginChainSignature(project)
  }

  watch([nativePlayback.required, () => editor.session, pendingPluginChain], ([required, project], [wasRequired]) => {
    if (!required || !project) {
      if (wasRequired && webOwnsPlayback()) playback.clearError()
      return
    }
    if (!webOwnsPlayback()) {
      if (!wasRequired) {
        stopRaf()
        releaseAudios()
      }
      return
    }
    const isCurrent = () => !disposed && project === editor.session && nativePlayback.required.value && webOwnsPlayback()
    playback.clearError()
    void nativePlayback.play(currentTime.value, {
      isCurrent,
      takePosition() {
        const position = clampTime(sampleCurrentTime())
        activeRequestId = playback.nextRequest()
        stopRaf()
        releaseAudios()
        return position
      },
    }).catch(cause => {
      if (isCurrent()) error.value = cause instanceof Error ? cause.message : String(cause)
    })
  })

  onBeforeUnmount(() => {
    disposed = true
    stop(true)
    releaseAudios()
    if (masterLimiterNode) {
      masterLimiterNode.onprocessorerror = null
      masterLimiterNode.disconnect()
      masterLimiterNode.port.close()
      masterLimiterNode = null
    }
    if (audioContext) {
      void audioContext.close().catch(() => undefined)
      audioContext = null
    }
  })

  return {
    intent,
    status,
    transportVisualState,
    transportPendingAction,
    transportCanToggle,
    isBusy,
    isActuallyPlaying,
    shouldFollowPlayhead,
    playbackError: error,
    currentTime,
    loop,
    level,
    masterLevel,
    trackLevels,
    requestId,
    requestPlay,
    requestPause,
    toggleTransport,
    stop,
    seek,
    followPlayhead,
    applyMasterVolume,
    preloadActiveTracks,
  }
}
