import { onBeforeUnmount, type Ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { convertFileSrc } from '@tauri-apps/api/core'
import i18n from '@/i18n'
import { clipIsActive, clipMediaTime, clipsForTrack } from '@/utils/editorClips'
import { createEditorAudioElement } from '@/utils/editorAudio'
import type { EditorClip, EditorSource, EditorTrack } from '@/types/editor'
import type { useEditorStore } from '@/stores/editor'
import { useEditorPlaybackStore } from '@/stores/editorPlayback'

type EditorStore = ReturnType<typeof useEditorStore>

type PlaybackOptions = {
  editor: EditorStore
  scrollEl: Ref<HTMLElement | null>
  trackHeaderWidth?: number
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
  audio: HTMLAudioElement
  metadataReady: boolean
  metadataPromise: Promise<void>
  fallbackUrl?: string | null
  graphEnabled?: boolean
  channelMode?: 'mono' | 'stereo'
  sourceNode?: MediaElementAudioSourceNode | null
  gainNode?: GainNode | null
  balanceSplitter?: ChannelSplitterNode | null
  balanceLeftGain?: GainNode | null
  balanceRightGain?: GainNode | null
  balanceMerger?: ChannelMergerNode | null
  clarityFilter?: BiquadFilterNode | null
  compressorNode?: DynamicsCompressorNode | null
  effectDryGain?: GainNode | null
  reverbNode?: ConvolverNode | null
  reverbWetGain?: GainNode | null
  delayNode?: DelayNode | null
  delayFeedbackGain?: GainNode | null
  delayWetGain?: GainNode | null
  effectOutputGain?: GainNode | null
  meterSplitter?: ChannelSplitterNode | null
  meterAnalyserLeft?: AnalyserNode | null
  meterAnalyserRight?: AnalyserNode | null
}

type FollowPlayheadMode = 'playback' | 'seek'

const ERROR_SESSION_NOT_LOADED = '请先加载编辑工程'
const ERROR_NO_PLAYABLE_TRACKS = '当前没有可播放的音轨'
const ERROR_NO_LOADED_AUDIO = '没有成功加载任何音频'
const ERROR_MISSING_ASSETS = () => String(i18n.global.t('editor.assetOfflineBlocked'))

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value))
}

export function useEditorPlayback(options: PlaybackOptions) {
  const { editor, scrollEl } = options
  const trackHeaderWidth = options.trackHeaderWidth ?? 0
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

  const audioEntries = new Map<string, ManagedAudio>()

  let rafId: number | null = null
  let playbackAnchorTime = 0
  let activeRequestId = 0
  let followScrollRafId: number | null = null
  let followScrollTargetLeft = 0
  let audioContext: AudioContext | null = null
  let masterInputGain: GainNode | null = null
  let masterBalanceSplitter: ChannelSplitterNode | null = null
  let masterBalanceLeftGain: GainNode | null = null
  let masterBalanceRightGain: GainNode | null = null
  let masterBalanceMerger: ChannelMergerNode | null = null
  let masterMeterSplitter: ChannelSplitterNode | null = null
  let masterMeterAnalyserLeft: AnalyserNode | null = null
  let masterMeterAnalyserRight: AnalyserNode | null = null
  let reverbImpulseBuffer: AudioBuffer | null = null
  const analyserBufferCache = new WeakMap<AnalyserNode, Uint8Array<ArrayBuffer>>()

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

  function ensureMeterBuffer(node: AnalyserNode) {
    let cached = analyserBufferCache.get(node)
    if (!cached) {
      cached = new Uint8Array<ArrayBuffer>(new ArrayBuffer(node.fftSize))
      analyserBufferCache.set(node, cached)
    }
    return cached
  }

  function analyserLevel(node?: AnalyserNode | null) {
    if (!node) return 0
    const buffer = ensureMeterBuffer(node)
    node.getByteTimeDomainData(buffer)
    let sum = 0
    for (let index = 0; index < buffer.length; index += 1) {
      const sample = (buffer[index] - 128) / 128
      sum += sample * sample
    }
    const rms = Math.sqrt(sum / Math.max(1, buffer.length))
    const boosted = Math.pow(clamp(rms * 3.2, 0, 1), 0.72)
    return clamp(boosted, 0, 1)
  }

  function stereoGainForPan(pan: number) {
    const normalized = clamp(Number(pan || 0), -1, 1)
    return {
      left: normalized <= 0 ? 1 : 1 - normalized,
      right: normalized >= 0 ? 1 : 1 + normalized,
    }
  }

  function getReverbImpulseBuffer(ctx: AudioContext) {
    if (reverbImpulseBuffer && reverbImpulseBuffer.sampleRate === ctx.sampleRate) return reverbImpulseBuffer
    const length = Math.max(1, Math.floor(ctx.sampleRate * 1.35))
    const buffer = ctx.createBuffer(2, length, ctx.sampleRate)
    for (let channel = 0; channel < buffer.numberOfChannels; channel += 1) {
      const data = buffer.getChannelData(channel)
      for (let index = 0; index < data.length; index += 1) {
        const decay = Math.pow(1 - index / data.length, 2.4)
        const stereoDelay = channel === 1 && index < Math.floor(ctx.sampleRate * 0.013)
        const noise = Math.sin((index + 1) * 12.9898 + channel * 78.233) * 43758.5453
        const unitNoise = (noise - Math.floor(noise)) * 2 - 1
        data[index] = stereoDelay ? 0 : unitNoise * decay * (index === 0 ? 0.7 : 0.22)
      }
    }
    reverbImpulseBuffer = buffer
    return buffer
  }

  function applyTrackEffects(entry: ManagedAudio, track: EditorTrack) {
    if (!entry.clarityFilter || !entry.compressorNode || !entry.effectDryGain || !entry.reverbWetGain || !entry.delayWetGain || !entry.delayNode || !entry.delayFeedbackGain) return
    const effects = track.effects
    const reverb = clamp(Number(effects?.reverb ?? 0), 0, 1)
    const delay = clamp(Number(effects?.delay ?? 0), 0, 1)
    const delayTime = clamp(Number(effects?.delayTime ?? 0.24), 0.05, 1.2)
    const clarity = clamp(Number(effects?.clarity ?? 0), 0, 1)
    const compressor = clamp(Number(effects?.compressor ?? 0), 0, 1)
    entry.clarityFilter.type = 'highpass'
    entry.clarityFilter.frequency.value = 20 + clarity * 180
    entry.clarityFilter.Q.value = 0.7
    entry.compressorNode.threshold.value = compressor > 0 ? -36 + compressor * 14 : -100
    entry.compressorNode.ratio.value = compressor > 0 ? 1 + compressor * 7 : 1
    entry.compressorNode.knee.value = compressor > 0 ? 18 : 0
    entry.compressorNode.attack.value = 0.003
    entry.compressorNode.release.value = 0.25
    entry.effectDryGain.gain.value = 1
    entry.reverbWetGain.gain.value = reverb * 0.45
    entry.delayWetGain.gain.value = delay * 0.38
    entry.delayNode.delayTime.value = delayTime
    entry.delayFeedbackGain.gain.value = delay * 0.52
  }

  function applyMasterAudioSettings() {
    if (!masterInputGain || !masterBalanceLeftGain || !masterBalanceRightGain) return
    masterInputGain.gain.value = clamp(Number(editor.masterVolume || 0), 0, 2)
    const stereo = stereoGainForPan(editor.masterPan)
    masterBalanceLeftGain.gain.value = stereo.left
    masterBalanceRightGain.gain.value = stereo.right
  }

  function applyTrackAudioSettings(entry: ManagedAudio, track: EditorTrack, baseVolume: number) {
    if (entry.gainNode && entry.balanceLeftGain && entry.balanceRightGain) {
      entry.gainNode.gain.value = clamp(Number(baseVolume || 0), 0, 2)
      const stereo = stereoGainForPan(track.pan)
      entry.balanceLeftGain.gain.value = stereo.left
      entry.balanceRightGain.gain.value = stereo.right
      applyTrackEffects(entry, track)
      entry.audio.volume = 1
      entry.audio.muted = false
      return
    }

    entry.audio.muted = baseVolume <= 0.0001
    entry.audio.volume = clamp(Number(baseVolume || 0), 0, 1)
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
    entry.audio.pause()
    entry.sourceNode?.disconnect()
    entry.gainNode?.disconnect()
    entry.balanceSplitter?.disconnect()
    entry.balanceLeftGain?.disconnect()
    entry.balanceRightGain?.disconnect()
    entry.balanceMerger?.disconnect()
    entry.clarityFilter?.disconnect()
    entry.compressorNode?.disconnect()
    entry.effectDryGain?.disconnect()
    entry.reverbNode?.disconnect()
    entry.reverbWetGain?.disconnect()
    entry.delayNode?.disconnect()
    entry.delayFeedbackGain?.disconnect()
    entry.delayWetGain?.disconnect()
    entry.effectOutputGain?.disconnect()
    entry.meterSplitter?.disconnect()
    entry.meterAnalyserLeft?.disconnect()
    entry.meterAnalyserRight?.disconnect()
    entry.audio.removeAttribute('src')
    entry.audio.load()
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
      entry.sourceNode = ctx.createMediaElementSource(entry.audio)
      entry.gainNode = ctx.createGain()
      entry.balanceSplitter = ctx.createChannelSplitter(2)
      entry.balanceLeftGain = ctx.createGain()
      entry.balanceRightGain = ctx.createGain()
      entry.balanceMerger = ctx.createChannelMerger(2)
      entry.clarityFilter = ctx.createBiquadFilter()
      entry.compressorNode = ctx.createDynamicsCompressor()
      entry.effectDryGain = ctx.createGain()
      entry.reverbNode = ctx.createConvolver()
      entry.reverbWetGain = ctx.createGain()
      entry.delayNode = ctx.createDelay(1.2)
      entry.delayFeedbackGain = ctx.createGain()
      entry.delayWetGain = ctx.createGain()
      entry.effectOutputGain = ctx.createGain()
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
      entry.reverbNode.buffer = getReverbImpulseBuffer(ctx)
      entry.balanceMerger.connect(entry.clarityFilter)
      entry.clarityFilter.connect(entry.compressorNode)
      entry.compressorNode.connect(entry.effectDryGain)
      entry.compressorNode.connect(entry.reverbNode)
      entry.compressorNode.connect(entry.delayNode)
      entry.delayNode.connect(entry.delayFeedbackGain)
      entry.delayFeedbackGain.connect(entry.delayNode)
      entry.effectDryGain.connect(entry.effectOutputGain)
      entry.reverbNode.connect(entry.reverbWetGain)
      entry.reverbWetGain.connect(entry.effectOutputGain)
      entry.delayNode.connect(entry.delayWetGain)
      entry.delayWetGain.connect(entry.effectOutputGain)
      entry.effectOutputGain.connect(entry.meterSplitter)
      entry.meterSplitter.connect(entry.meterAnalyserLeft, 0)
      entry.meterSplitter.connect(entry.meterAnalyserRight, 1)
      entry.effectOutputGain.connect(masterInputGain)
      entry.graphEnabled = true
    } catch {
      entry.graphEnabled = false
      entry.sourceNode = null
      entry.gainNode = null
      entry.balanceSplitter = null
      entry.balanceLeftGain = null
      entry.balanceRightGain = null
      entry.balanceMerger = null
      entry.clarityFilter = null
      entry.compressorNode = null
      entry.effectDryGain = null
      entry.reverbNode = null
      entry.reverbWetGain = null
      entry.delayNode = null
      entry.delayFeedbackGain = null
      entry.delayWetGain = null
      entry.effectOutputGain = null
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
    const session = editor.session
    if (!session) return false
    const hasSolo = session.tracks.some((track) => track.solo)
    const suppressedTrackIds = new Set(playback.suppressedTrackIds)
    // Only block playback for tracks that would actually be audible.
    // A muted, suppressed, or solo-excluded offline clip must not prevent previewing the rest.
    return session.tracks
      .filter((track) => !track.muted && !suppressedTrackIds.has(track.id) && (!hasSolo || track.solo))
      .some((track) => clipsForTrack(track, editor.sourceMap).some((clip) => (
        !clip.muted
        && clip.duration > 0
        && Boolean(editor.sourceMap.get(clip.assetId)?.missing)
      )))
  }

  function trackSignature() {
    return activeClips()
      .map(({ track, clip, source }) => [
        track.id,
        clip.id,
        source.id,
        source.channels,
        track.volume,
        track.pan,
        track.effects?.reverb ?? 0,
        track.effects?.delay ?? 0,
        track.effects?.delayTime ?? 0.24,
        track.effects?.clarity ?? 0,
        track.effects?.compressor ?? 0,
        track.muted,
        track.solo,
        clip.start,
        clip.offset,
        clip.duration,
        clip.volume,
        clip.fadeIn,
        clip.fadeOut,
        clip.muted,
      ].join(':'))
      .join('|')
  }

  function ensureAudioEntry(track: EditorTrack, clip: EditorClip, source: EditorSource) {
    const cached = audioEntries.get(clip.id)
    if (cached && cached.sourceId === source.id) {
      connectEntryAudioGraph(cached, source)
      return cached
    }

    if (cached) {
      releaseEntry(cached)
      audioEntries.delete(clip.id)
    }

    const primaryUrl = resolveAudioUrl(source.path)
    const fallbackUrl = resolveFileFallbackUrl(source.path)
    const audio = createEditorAudioElement(primaryUrl)

    const entry: ManagedAudio = {
      clipId: clip.id,
      trackId: track.id,
      sourceId: source.id,
      audio,
      fallbackUrl: fallbackUrl && fallbackUrl !== primaryUrl ? fallbackUrl : null,
      graphEnabled: undefined,
      metadataReady: audio.readyState >= 1,
      metadataPromise: Promise.resolve(),
    }

    audio.addEventListener('error', () => {
      if (!entry.fallbackUrl || entry.audio.currentSrc === entry.fallbackUrl || entry.audio.src === entry.fallbackUrl) return
      entry.metadataReady = false
      entry.audio.src = entry.fallbackUrl
      entry.audio.load()
    })

    entry.metadataPromise = createMetadataPromise(audio, entry)
    audioEntries.set(clip.id, entry)
    connectEntryAudioGraph(entry, source)
    audio.load()
    return entry
  }

  function setAudioTime(audio: HTMLAudioElement, time: number) {
    const maxTime = Number.isFinite(audio.duration) && audio.duration > 0
      ? Math.max(0, audio.duration - 0.01)
      : time
    const next = Math.max(0, Math.min(time, maxTime))
    try {
      audio.currentTime = next
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

    for (const { track, clip, source } of entries) {
      const entry = ensureAudioEntry(track, clip, source)
      const active = clipIsActive(clip, time)
      const volume = computeClipVolume(track, clip, time)
      applyTrackAudioSettings(entry, track, volume)
      if (!active) {
        if (!entry.audio.paused) entry.audio.pause()
        continue
      }

      const mediaTime = clipMediaTime(clip, time)
      if (Math.abs(entry.audio.currentTime - mediaTime) > 0.16) setAudioTime(entry.audio, mediaTime)
      if (playNow && entry.audio.paused) {
        void entry.audio.play().catch(() => undefined)
      } else if (!playNow && !entry.audio.paused) {
        entry.audio.pause()
      }
    }

    pauseInactiveAudios(knownIds)
    playback.clearTrackLevels([...audibleTrackIds])
  }

  async function preloadActiveTracks() {
    const entries = activeClips().map(({ track, clip, source }) => ensureAudioEntry(track, clip, source))
    await Promise.allSettled(entries.map((entry) => entry.metadataPromise))
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
    const el = scrollEl.value
    if (!el) return
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

  function stopRaf() {
    if (rafId !== null) {
      cancelAnimationFrame(rafId)
      rafId = null
    }
    stopFollowScroll()
  }

  function pauseAllAudios() {
    audioEntries.forEach((entry) => {
      entry.audio.pause()
    })
  }

  function syncPauseAt(time: number) {
    pauseAllAudios()
    for (const { track, clip, source } of activeClips()) {
      const entry = ensureAudioEntry(track, clip, source)
      setAudioTime(entry.audio, clipMediaTime(clip, time))
    }
  }

  function sampleCurrentTime() {
    if (status.value !== 'playing') return currentTime.value
    return Math.max(0, performance.now() / 1000 - playbackAnchorTime)
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
    const nextTime = clampTime(time)
    playback.setCurrentTime(nextTime)
    playbackAnchorTime = performance.now() / 1000 - nextTime
    applyClipStates(nextTime, false)

    const entries = activeClips()
    for (const { track, clip, source } of entries) {
      const entry = ensureAudioEntry(track, clip, source)
      const applySeek = () => setAudioTime(entry.audio, clipMediaTime(clip, nextTime))
      if (entry.metadataReady) applySeek()
      else void entry.metadataPromise.then(() => {
        if (entry.sourceId !== source.id) return
        applySeek()
      })
    }
  }

  async function syncActiveAudios(playNow: boolean, targetTime = currentTime.value, expectedRequestId = activeRequestId) {
    const entries = activeClips()
    if (!entries.length) {
      pauseAllAudios()
      return 0
    }

    const ctx = ensureAudioContext()
    if (playNow && ctx && ctx.state !== 'running') {
      await ctx.resume().catch(() => undefined)
    }

    const activeIds = new Set(entries.map(({ clip }) => clip.id))
    pauseInactiveAudios(activeIds)

    const managed = entries.map(({ track, clip, source }) => ({
      track,
      clip,
      source,
      entry: ensureAudioEntry(track, clip, source),
    }))

    const nextTime = clampTime(targetTime)
    playback.setCurrentTime(nextTime)
    playbackAnchorTime = performance.now() / 1000 - nextTime

    for (const { entry, clip } of managed) {
      setAudioTime(entry.audio, clipMediaTime(clip, nextTime))
      if (!entry.metadataReady) {
        void entry.metadataPromise.then(() => {
          if (expectedRequestId !== activeRequestId) return
          setAudioTime(entry.audio, clipMediaTime(clip, nextTime))
        })
      }
    }

    applyClipStates(nextTime, false)

    if (!playNow) return managed.length

    const activeManaged = managed.filter(({ clip }) => clipIsActive(clip, nextTime))
    const results = await Promise.allSettled(activeManaged.map(({ entry }) => entry.audio.play()))
    if (expectedRequestId !== activeRequestId) return 0
    if (activeManaged.length && !results.some((result) => result.status === 'fulfilled')) {
      const firstError = results.find((result): result is PromiseRejectedResult => result.status === 'rejected')
      throw firstError?.reason || new Error(ERROR_NO_LOADED_AUDIO)
    }
    return managed.length
  }

  async function requestPlay(offset = currentTime.value) {
    playback.clearError()

    if (!editor.session) {
      const id = playback.beginRequest('pause', 'paused')
      activeRequestId = id
      playback.fail(id, ERROR_SESSION_NOT_LOADED)
      return false
    }

    if (hasMissingSourcesInUse()) {
      const id = playback.beginRequest('pause', 'paused')
      activeRequestId = id
      playback.fail(id, ERROR_MISSING_ASSETS())
      return false
    }

    if (!activeClips().length) {
      const id = playback.beginRequest('pause', 'paused')
      activeRequestId = id
      playback.fail(id, ERROR_NO_PLAYABLE_TRACKS)
      return false
    }

    const id = playback.beginRequest('play', 'starting')
    activeRequestId = id

    const playableCount = await syncActiveAudios(true, offset, id).catch((cause) => {
      if (id !== activeRequestId) return -1
      playback.fail(id, cause instanceof Error ? cause.message : String(cause))
      return 0
    })

    if (id !== activeRequestId) return false
    if (intent.value !== 'play') return false

    if (playableCount <= 0) {
      if (!error.value) playback.fail(id, ERROR_NO_LOADED_AUDIO)
      return false
    }

    if (!playback.finishPlay(id)) return false
    playbackAnchorTime = performance.now() / 1000 - currentTime.value
    followPlayhead('seek')
    stopRaf()
    rafId = requestAnimationFrame(tick)
    return true
  }

  function requestPause(reset = false) {
    const nextTime = reset ? 0 : clampTime(sampleCurrentTime())
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
    updateLevelMeter()
    followPlayhead()

    const total = computeDuration()
    if (total > 0 && time >= total - 0.04) {
      if (loop.value && intent.value === 'play') {
        const id = playback.beginRequest('play', 'starting')
        activeRequestId = id
        void syncActiveAudios(true, 0, id).then((count) => {
          if (id !== activeRequestId || intent.value !== 'play') return
          if (count <= 0) {
            playback.fail(id, ERROR_NO_LOADED_AUDIO)
            return
          }
          if (!playback.finishPlay(id)) return
          playback.setCurrentTime(0)
          playbackAnchorTime = performance.now() / 1000
          followPlayhead('seek')
          stopRaf()
          rafId = requestAnimationFrame(tick)
        }).catch((cause) => {
          playback.fail(id, cause instanceof Error ? cause.message : String(cause))
        })
        return
      }

      requestPause(true)
      return
    }

    rafId = requestAnimationFrame(tick)
  }

  function seek(time: number) {
    const next = clampTime(time)
    syncPausedAudiosToTime(next)

    if (intent.value === 'play') {
      followPlayhead('seek')
      const id = playback.beginRequest('play', 'starting')
      activeRequestId = id
      void syncActiveAudios(true, next, id).then((count) => {
        if (id !== activeRequestId || intent.value !== 'play') return
        if (count <= 0) {
          playback.fail(id, ERROR_NO_LOADED_AUDIO)
          return
        }
        if (!playback.finishPlay(id)) return
        stopRaf()
        rafId = requestAnimationFrame(tick)
      }).catch((cause) => {
        playback.fail(id, cause instanceof Error ? cause.message : String(cause))
      })
    }
  }

  function applyMasterVolume() {
    applyMasterAudioSettings()
    applyClipStates(currentTime.value, status.value === 'playing')
  }

  watch(() => editor.masterVolume, () => {
    applyMasterVolume()
  })

  watch(() => editor.masterPan, () => {
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
    () => trackSignature(),
    () => {
      if (intent.value === 'play') {
        const id = playback.beginRequest('play', 'starting')
        activeRequestId = id
        void syncActiveAudios(true, currentTime.value, id).then((count) => {
          if (id !== activeRequestId || intent.value !== 'play') return
          if (count <= 0) {
            playback.fail(id, ERROR_NO_LOADED_AUDIO)
            return
          }
          if (!playback.finishPlay(id)) return
          stopRaf()
          rafId = requestAnimationFrame(tick)
        }).catch((cause) => {
          playback.fail(id, cause instanceof Error ? cause.message : String(cause))
        })
      } else {
        syncPausedAudiosToTime(currentTime.value)
        void preloadActiveTracks()
      }
    },
    { immediate: true },
  )

  onBeforeUnmount(() => {
    stop(true)
    releaseAudios()
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
