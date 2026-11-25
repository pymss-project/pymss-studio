import { invoke } from '@tauri-apps/api/core'
import type { EditorClip, EditorSource } from '../types/editor'

/**
 * Create an audio element for editor playback.
 *
 * The CORS mode must be set before assigning `src`. Tauri serves linked
 * assets through its custom asset protocol; WebKit otherwise treats the
 * media request as no-cors and a MediaElementAudioSourceNode outputs silence.
 */
export function createEditorAudioElement(url: string): HTMLAudioElement {
  const audio = new Audio()
  audio.crossOrigin = 'anonymous'
  audio.preload = 'auto'
  audio.loop = false
  audio.src = url
  return audio
}

/** Limit native decoders so prefetching six stems cannot flood the machine. */
export function createPreviewChunkReader(context: BaseAudioContext) {
  let active = 0
  const pending: Array<() => void> = []
  const cancelled = () => new DOMException('Preview decoding cancelled', 'AbortError')
  async function enter(signal?: AbortSignal) {
    if (signal?.aborted) throw cancelled()
    if (active < 4) { active++; return }
    await new Promise<void>((resolve, reject) => {
      const advance = () => { signal?.removeEventListener('abort', abort); resolve() }
      const abort = () => {
        const index = pending.indexOf(advance)
        if (index >= 0) pending.splice(index, 1)
        reject(cancelled())
      }
      pending.push(advance)
      signal?.addEventListener('abort', abort, { once: true })
    })
  }
  function leave() {
    const next = pending.shift()
    if (next) next()
    else active--
  }
  return async (source: EditorSource, index: number, signal?: AbortSignal) => {
    await enter(signal)
    try {
      if (signal?.aborted) throw cancelled()
      // All chunks use the output clock's rate. Independent WebKit resampling
      // of source-rate buffers cannot interpolate across their boundaries.
      const sampleRate = context.sampleRate
      const channels = source.channels === 1 ? 1 : 2
      const bytes = await invoke<ArrayBuffer | number[]>('editor_preview_chunk', {
        path: source.path, index, sampleRate, channels,
      })
      // An in-flight native call keeps its decoder slot until completion. A
      // cancelled request must not allocate PCM buffers or release that slot early.
      if (signal?.aborted) throw cancelled()
      const data = bytes instanceof ArrayBuffer ? bytes : new Uint8Array(bytes).buffer
      if (data.byteLength % (channels * 4)) throw new Error('Invalid preview PCM data')
      const frames = data.byteLength / (channels * 4)
      if (!frames) {
        if (index === 0) throw new Error('Audio source contains no preview audio')
        // FFmpeg completed successfully. Compressed-file duration estimates
        // can extend beyond the last decoded sample; a later empty chunk is EOF.
        return null
      }
      const buffer = context.createBuffer(channels, frames, sampleRate)
      const interleaved = new DataView(data)
      for (let channel = 0; channel < channels; channel++) {
        const samples = buffer.getChannelData(channel)
        for (let frame = 0; frame < frames; frame++) {
          samples[frame] = interleaved.getFloat32((frame * channels + channel) * 4, true)
        }
      }
      return buffer
    } finally { leave() }
  }
}

export const PREVIEW_CHUNK_SECONDS = 4
const START_LEAD = 0.06
const PREFETCH_SECONDS = 12
const CACHE_BYTES = 64 * 1024 * 1024

export type TimelineAudioClip = { clip: EditorClip; source: EditorSource; output: AudioNode }
/** null is a successful EOF result; decoder and PCM validation failures reject. */
export type PreviewChunkReader = (source: EditorSource, index: number, signal?: AbortSignal) => Promise<AudioBuffer | null>
type Segment = { entry: TimelineAudioClip; index: number; start: number; end: number; offset: number }
type Slice = { node: AudioBufferSourceNode; gain: GainNode; clipId: string; start: number; end: number }
// null bytes means pending decoding; zero bytes means settled EOF.
type CacheEntry = { promise: Promise<AudioBuffer | null>; bytes: number | null; controller: AbortController }

/** All positions are project seconds; only this transport maps them to audio time. */
export class EditorTimelinePlayback {
  private readonly context: AudioContext
  private readonly readChunk: PreviewChunkReader
  private readonly onError: (error: unknown) => void
  private readonly onBuffering: (waiting: boolean) => void
  private readonly onEnded: () => void
  private entries: TimelineAudioClip[] = []
  private slices = new Set<Slice>()
  private cache = new Map<string, CacheEntry>()
  private sourceEnds = new Map<string, number>()
  private cacheBytes = 0
  private origin = 0
  private offset = 0
  private coveredUntil = 0
  private duration = 0
  private running = false
  private generation = 0
  private cancelStart: (() => void) | null = null
  private timer: ReturnType<typeof setInterval> | null = null
  private loadingAhead = false
  private buffering = false

  constructor(context: AudioContext, readChunk: PreviewChunkReader,
    onError: (error: unknown) => void, onBuffering: (waiting: boolean) => void = () => {},
    onEnded: () => void = () => {}) {
    this.context = context
    this.readChunk = readChunk
    this.onError = onError
    this.onBuffering = onBuffering
    this.onEnded = onEnded
  }

  get currentTime() {
    if (!this.running) return this.offset
    const position = Math.min(this.coveredUntil, this.offset + this.context.currentTime - this.origin)
    return Math.min(this.duration, Math.max(this.offset, position))
  }
  get bufferedUntil() { return this.coveredUntil }
  get isPlaying() { return this.running && !this.buffering }

  async preload(entries: TimelineAudioClip[], offset: number, duration: number) {
    // A delayed background warmup must not replace the initial window of a
    // foreground play/seek that is already waiting for decoding.
    if (this.cancelStart) return
    const segments = this.segments(entries, offset, Math.min(duration, offset + PREVIEW_CHUNK_SECONDS))
    if (!this.running) this.cancelPending(new Set(segments.map(({ entry, index }) => this.key(entry.source, index))))
    await this.prepare(segments)
  }

  async start(entries: TimelineAudioClip[], offset: number, duration: number): Promise<boolean> {
    const rate = this.context.sampleRate
    offset = Math.round(offset * rate) / rate
    this.pause(true)
    this.setDuration(duration)
    const until = Math.min(this.duration, offset + PREVIEW_CHUNK_SECONDS)
    const segments = this.segments(entries, offset, until)
    this.cancelPending(new Set(segments.map(({ entry, index }) => this.key(entry.source, index))))
    const generation = this.generation
    this.entries = entries
    this.offset = offset
    const cancelled = new Promise<null>(resolve => { this.cancelStart = () => resolve(null) })
    let ready: Map<string, AudioBuffer | null> | null
    try { ready = await Promise.race([this.prepare(segments), cancelled]) }
    catch (error) {
      if (generation !== this.generation) return false
      this.pause()
      throw error
    }
    if (!ready || generation !== this.generation) return false
    this.cancelStart = null
    try { this.activate(segments, ready, until) }
    catch (error) { this.pause(); throw error }
    void this.pump()
    return true
  }

  private activate(segments: Segment[], ready: Map<string, AudioBuffer | null>, until: number) {
    // Choose this timestamp once, after ALL initial clips are ready.
    this.origin = Math.round((this.context.currentTime + START_LEAD) * this.context.sampleRate) / this.context.sampleRate
    this.coveredUntil = until
    this.running = true
    this.schedule(segments, ready)
    this.timer = setInterval(() => { void this.pump() }, 100)
  }

  pause(keepPending = false) {
    this.offset = this.currentTime
    this.running = false
    this.buffering = false
    this.generation++
    this.cancelStart?.()
    this.cancelStart = null
    this.loadingAhead = false
    if (this.timer !== null) clearInterval(this.timer)
    this.timer = null
    this.clearSlices()
    if (!keepPending) this.cancelPending()
  }

  private cancelPending(keep?: Set<string>) {
    for (const [key, entry] of this.cache) {
      if (entry.bytes !== null || keep?.has(key)) continue
      entry.controller.abort()
      this.cache.delete(key)
    }
  }

  dispose() {
    this.pause()
    this.cache.clear()
    this.sourceEnds.clear()
    this.cacheBytes = 0
  }

  setDuration(duration: number) {
    const rate = this.context.sampleRate
    this.duration = Math.round(duration * rate) / rate
  }

  updateMix(entries: TimelineAudioClip[]) {
    // Editor patches may replace clip objects. Future chunks must use the
    // latest objects too, not only update gains on already scheduled slices.
    this.entries = entries
    const clips = new Map(entries.map(entry => [entry.clip.id, entry.clip]))
    for (const slice of this.slices) {
      const clip = clips.get(slice.clipId)
      if (clip) this.automateGain(slice, clip)
    }
  }

  /** Independently of rAF: queue all clips in the next window as one group. */
  async pump() {
    if (this.running && this.coveredUntil >= this.duration
      && this.offset + this.context.currentTime - this.origin >= this.duration) {
      // Completion belongs to the audio clock too. rAF can stop when the
      // window is hidden, but the timer and transport must still finish/loop.
      this.pause()
      this.onEnded()
      return
    }
    if (this.running && this.loadingAhead && !this.buffering
      && this.offset + this.context.currentTime - this.origin >= this.coveredUntil) {
      this.buffering = true
      this.onBuffering(true)
    }
    if (!this.running || this.loadingAhead || this.coveredUntil >= this.duration
      || this.coveredUntil - this.currentTime >= PREFETCH_SECONDS) return
    const from = this.coveredUntil
    const until = Math.min(this.duration, from + PREVIEW_CHUNK_SECONDS)
    const segments = this.segments(this.entries, from, until)
    if (!segments.length) {
      // Silent windows need no decoding or change to the playback clock.
      this.coveredUntil = until
      return
    }
    this.loadingAhead = true
    const generation = this.generation
    let operationGeneration = generation
    try {
      const ready = await this.prepare(segments)
      if (generation !== this.generation || !this.running) return
      const clockPosition = this.offset + this.context.currentTime - this.origin
      if (clockPosition >= from) {
        // Never schedule overdue audio at "now" or skip it to catch up.
        // Stop the entire mix at its shared boundary, then resume that position.
        const wasBuffering = this.buffering
        this.pause()
        this.buffering = wasBuffering
        this.offset = from
        operationGeneration = this.generation
        if (!this.buffering) { this.buffering = true; this.onBuffering(true) }
        if (operationGeneration !== this.generation) return
        // Reuse the entire prepared group. Re-reading an evicted cache entry
        // here could fail after changing generation and leave startup stuck.
        this.activate(segments, ready, until)
        this.buffering = false
        this.onBuffering(false)
        return
      }
      this.schedule(segments, ready)
      this.coveredUntil = until
    } catch (error) {
      if (operationGeneration !== this.generation) return
      this.pause()
      this.onError(error)
    } finally {
      if (operationGeneration === this.generation) this.loadingAhead = false
    }
  }

  private segments(entries: TimelineAudioClip[], from: number, until: number): Segment[] {
    const result: Segment[] = []
    // Clip positions, seeks and chunk boundaries share an integer sample grid.
    // Fractional starts would give each buffer node a different interpolation
    // phase, even when its buffer already uses the output sample rate.
    const rate = this.context.sampleRate
    const chunkFrames = PREVIEW_CHUNK_SECONDS * rate
    const fromFrame = Math.round(from * rate), untilFrame = Math.round(until * rate)
    for (const entry of entries) {
      const { clip } = entry
      // Source metadata is hydrated in the background. One decode window must
      // keep the same path/rate/channels for its cache key and native request.
      const source = { ...entry.source }
      const snapshot = { ...entry, source }
      const clipStart = Math.round(clip.start * rate), clipOffset = Math.round(clip.offset * rate)
      let start = Math.max(fromFrame, clipStart)
      const sourceEnd = Math.min(source.duration, this.sourceEnds.get(this.sourceKey(source)) ?? Infinity)
      const end = Math.min(untilFrame, Math.round((clip.start + clip.duration) * rate),
        clipStart + Math.max(0, Math.round(sourceEnd * rate) - clipOffset))
      while (start < end) {
        const mediaFrame = clipOffset + start - clipStart
        const index = Math.floor(mediaFrame / chunkFrames)
        const boundary = clipStart - clipOffset + (index + 1) * chunkFrames
        const segmentEnd = Math.min(end, boundary)
        result.push({ entry: snapshot, index, start: start / rate, end: segmentEnd / rate,
          offset: (mediaFrame - index * chunkFrames) / rate })
        start = segmentEnd
      }
    }
    return result
  }

  // Duration separates a file replaced at the same path once its metadata is refreshed.
  private sourceKey(source: EditorSource) {
    return JSON.stringify([source.path, source.sampleRate, source.channels, source.duration])
  }

  private key(source: EditorSource, index: number) { return `${this.sourceKey(source)}:${index}` }

  private async prepare(segments: Segment[]) {
    const ready = new Map<string, AudioBuffer | null>()
    await Promise.all(segments.map(async ({ entry: { source }, index }) => {
      const key = this.key(source, index)
      let cached = this.cache.get(key)
      if (!cached) {
        const controller = new AbortController()
        const created: CacheEntry = { bytes: null, controller,
          promise: this.readChunk(source, index, controller.signal) }
        cached = created
        this.cache.set(key, created)
        void created.promise.then(buffer => {
          if (this.cache.get(key) !== created) return
          created.bytes = buffer ? buffer.length * buffer.numberOfChannels * 4 : 0
          this.cacheBytes += created.bytes
          if (!buffer || buffer.duration < PREVIEW_CHUNK_SECONDS) {
            const sourceKey = this.sourceKey(source)
            const end = index * PREVIEW_CHUNK_SECONDS + (buffer?.duration ?? 0)
            this.sourceEnds.set(sourceKey, Math.min(this.sourceEnds.get(sourceKey) ?? Infinity, end))
          }
          this.evict()
        }, () => { if (this.cache.get(key) === created) this.cache.delete(key) })
      } else {
        this.cache.delete(key)
        this.cache.set(key, cached)
      }
      ready.set(key, await cached.promise)
    }))
    return ready
  }

  private evict() {
    for (const [key, cached] of this.cache) {
      if (this.cacheBytes <= CACHE_BYTES) break
      if (cached.bytes === null) continue
      this.cache.delete(key)
      this.cacheBytes -= cached.bytes
    }
  }

  private schedule(segments: Segment[], ready: Map<string, AudioBuffer | null>) {
    const clips = new Map(this.entries.map(entry => [entry.clip.id, entry.clip]))
    for (const segment of segments) {
      const { entry, index, start, end, offset } = segment
      const buffer = ready.get(this.key(entry.source, index))!
      if (!buffer) continue
      const rate = this.context.sampleRate
      const offsetFrames = Math.round(offset * rate)
      const frames = Math.min(Math.round(end * rate) - Math.round(start * rate), buffer.length - offsetFrames)
      const length = frames / rate
      if (length <= 0) continue
      const node = this.context.createBufferSource()
      const gain = this.context.createGain()
      const slice: Slice = { node, gain, clipId: entry.clip.id, start, end: start + length }
      try {
        node.buffer = buffer
        node.connect(gain)
        gain.connect(entry.output)
        this.automateGain(slice, clips.get(entry.clip.id) || entry.clip)
        node.onended = () => {
          this.slices.delete(slice)
          node.disconnect()
          gain.disconnect()
        }
        const when = (Math.round(this.origin * rate) + Math.round(start * rate) - Math.round(this.offset * rate)) / rate
        node.start(when, offsetFrames / rate, length)
        this.slices.add(slice)
      } catch (error) {
        // Only successfully started nodes may later be stopped. Preserve the
        // startup failure rather than masking it with stop() on an unstarted node.
        node.onended = null
        node.disconnect()
        gain.disconnect()
        throw error
      }
    }
  }

  private automateGain(slice: Slice, clip: EditorClip) {
    const start = Math.max(slice.start, this.currentTime)
    const end = slice.end
    if (end <= start) return
    scheduleEditorClipGain(slice.gain.gain, clip, start, end, this.origin, this.offset)
  }

  private clearSlices() {
    for (const { node, gain } of this.slices) {
      node.onended = null
      node.stop()
      node.disconnect()
      gain.disconnect()
    }
    this.slices.clear()
  }
}

export function scheduleEditorClipGain(param: AudioParam, clip: EditorClip, start: number, end: number, origin: number, offset = 0) {
  const when = origin + start - offset
  const fadeIn = Math.min(clip.duration, Math.max(0, clip.fadeIn || 0))
  const fadeOut = Math.min(clip.duration, Math.max(0, clip.fadeOut || 0))
  const clipEnd = clip.start + clip.duration
  const gainAt = (time: number) => Math.max(0, clip.volume)
    * (fadeIn > 0 ? Math.min(1, Math.max(0, (time - clip.start) / fadeIn)) : 1)
    * (fadeOut > 0 ? Math.min(1, Math.max(0, (clipEnd - time) / fadeOut)) : 1)
  const points = [start, end]
  for (const point of [clip.start + fadeIn, clipEnd - fadeOut]) {
    if (point > start && point < end) points.push(point)
  }
  // Cancel the previous curve from its start too, so a live edit cannot
  // overlap an in-flight setValueCurveAtTime event.
  param.cancelScheduledValues(0)
  points.sort((a, b) => a - b)
  if (fadeIn > 0 && fadeOut > 0 && fadeIn + fadeOut > clip.duration) {
    // Export and the original preview multiply the two fades. Their overlap
    // is curved. Keep the fade corners exact, even when one fade is much
    // shorter than the whole chunk. Adjacent curves share an endpoint.
    let curveStart = when
    for (let index = 1; index < points.length; index++) {
      const left = points[index - 1], right = points[index]
      const middle = (left + right) / 2
      const curved = middle < clip.start + fadeIn && middle > clipEnd - fadeOut
      const length = curved ? 257 : 2
      const values = Float32Array.from({ length }, (_, sample) => gainAt(left + (right - left) * sample / (length - 1)))
      const duration = origin + right - offset - curveStart
      if (duration <= 0) continue
      param.setValueCurveAtTime(values, curveStart, duration)
      // Use the same addition as the native event's end, preventing tiny
      // floating-point overlaps between otherwise adjacent curve events.
      curveStart += duration
    }
    return
  }
  param.setValueAtTime(gainAt(start), when)
  for (const point of points.slice(1)) {
    param.linearRampToValueAtTime(gainAt(point), origin + point - offset)
  }
}
