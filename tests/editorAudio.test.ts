import assert from 'node:assert/strict'
import test, { describe, it } from 'node:test'
import { createEditorAudioElement, createPreviewChunkReader, EditorTimelinePlayback, type TimelineAudioClip, type PreviewChunkReader } from '../src/utils/editorAudio.ts'
import type { EditorSource } from '../src/types/editor.ts'

describe('editor audio element setup', () => {
  it('sets CORS before assigning the asset URL', () => {
    const originalAudio = globalThis.Audio
    const operations: string[] = []

    class FakeAudio {
      private _crossOrigin = ''
      private _preload = ''
      private _loop = false
      private _src = ''

      get crossOrigin() { return this._crossOrigin }
      set crossOrigin(value: string) {
        operations.push('crossOrigin')
        this._crossOrigin = value
      }

      get preload() { return this._preload }
      set preload(value: string) {
        operations.push('preload')
        this._preload = value
      }

      get loop() { return this._loop }
      set loop(value: boolean) {
        operations.push('loop')
        this._loop = value
      }

      get src() { return this._src }
      set src(value: string) {
        operations.push('src')
        this._src = value
      }
    }

    Object.defineProperty(globalThis, 'Audio', {
      configurable: true,
      value: FakeAudio,
    })

    try {
      const audio = createEditorAudioElement('asset://localhost/audio.wav')
      assert.deepEqual(operations, ['crossOrigin', 'preload', 'loop', 'src'])
      assert.equal(audio.crossOrigin, 'anonymous')
      assert.equal(audio.preload, 'auto')
      assert.equal(audio.loop, false)
      assert.equal(audio.src, 'asset://localhost/audio.wav')
    } finally {
      Object.defineProperty(globalThis, 'Audio', {
        configurable: true,
        value: originalAudio,
      })
    }
  })
})

const source = { path: '/tone.flac', sampleRate: 44100, channels: 2 } as EditorSource
function setup(t: any, invoke: (command: string, args: any) => Promise<ArrayBuffer | number[]>) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { __TAURI_INTERNALS__: { invoke } } })
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, 'window', previous)
    else Reflect.deleteProperty(globalThis, 'window')
  })
  const context = {
    sampleRate: 48000,
    createBuffer(channels: number, frames: number, sampleRate: number) {
      const data = Array.from({ length: channels }, () => new Float32Array(frames))
      return { length: frames, sampleRate, numberOfChannels: channels, getChannelData: (index: number) => data[index] }
    },
  }
  return createPreviewChunkReader(context as unknown as AudioContext)
}

test('native PCM uses the output clock rate and deinterleaves channels without a second resampling', async t => {
  let request: any
  const raw = new Float32Array([0.1, 0.2, 0.3, 0.4])
  const read = setup(t, async (command, args) => {
    assert.equal(command, 'editor_preview_chunk'); request = args; return raw.buffer
  })
  const buffer = await read(source, 3)
  assert.ok(buffer)
  assert.deepEqual(request, { path: source.path, index: 3, sampleRate: 48000, channels: 2 })
  assert.equal(buffer.sampleRate, 48000)
  assert.equal(buffer.length, 2)
  assert.deepEqual(buffer.getChannelData(0), new Float32Array([0.1, 0.3]))
  assert.deepEqual(buffer.getChannelData(1), new Float32Array([0.2, 0.4]))
})

test('prefetch limits native decoder concurrency to four and drains queued chunks', async t => {
  const pending: Array<() => void> = []
  let active = 0, peak = 0
  const read = setup(t, () => {
    active++; peak = Math.max(peak, active)
    return new Promise(resolve => pending.push(() => { active--; resolve(new Float32Array([1, 1]).buffer) }))
  })
  const chunks = Array.from({ length: 8 }, (_, index) => read(source, index))
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(pending.length, 4)
  for (const finish of pending.slice(0, 4)) finish()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(pending.length, 8)
  for (const finish of pending.slice(4)) finish()
  await Promise.all(chunks)
  assert.equal(peak, 4)
  assert.equal(active, 0)
})

test('only successful later empty chunks report EOF; empty first chunks and malformed PCM fail', async t => {
  let response = new ArrayBuffer(0)
  const read = setup(t, async () => response)
  await assert.rejects(read(source, 0), /contains no preview audio/)
  assert.equal(await read(source, 3), null)
  response = new ArrayBuffer(3)
  await assert.rejects(read(source, 0), /Invalid preview PCM/)
  await assert.rejects(read(source, 3), /Invalid preview PCM/)
})

test('native decoder failures at later positions remain errors', async t => {
  const read = setup(t, async () => { throw new Error('Preview decode failed: invalid audio') })
  await assert.rejects(read(source, 3), /Preview decode failed: invalid audio/)
})

const flush = async () => { await new Promise(resolve => setImmediate(resolve)) }
function gate<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
function buffer(seconds = 4, sampleRate = 48000) {
  const length = Math.round(seconds * sampleRate)
  return { duration: length / sampleRate, length, sampleRate, numberOfChannels: 2 } as AudioBuffer
}
function fixture(t: any, reader: PreviewChunkReader = async () => buffer()) {
  const nodes: any[] = [], gains: any[] = []
  const context = {
    currentTime: 0, sampleRate: 48000,
    createBufferSource() {
      const node = { buffer: null, onended: null, stopped: false, starts: [] as number[][],
        connect(destination: any) { this.destination = destination }, destination: null,
        disconnect() {}, stop() { this.stopped = true },
        start(...args: number[]) { this.starts.push(args) },
      }
      nodes.push(node); return node
    },
    createGain() {
      const gain = { output: null, events: [] as number[][],
        curves: [] as Array<{ values: Float32Array; when: number; duration: number }>,
        cancelled: [] as number[],
        connect(output: any) { this.output = output }, disconnect() {},
        gain: { cancelScheduledValues(when: number) { gain.cancelled.push(when) },
          setValueAtTime(value: number, at: number) { gain.events.push([value, at]) },
          linearRampToValueAtTime(value: number, at: number) { gain.events.push([value, at]) },
          setValueCurveAtTime(values: Float32Array, when: number, duration: number) {
            gain.curves.push({ values, when, duration })
          },
        },
      }
      gains.push(gain); return gain
    },
  }
  const errors: unknown[] = [], buffering: boolean[] = [], ended: number[] = []
  const transport = new EditorTimelinePlayback(context as unknown as AudioContext, reader,
    error => errors.push(error), waiting => buffering.push(waiting), () => ended.push(transport.currentTime))
  t.after(() => transport.dispose())
  function entry(id = 'clip', start = 0, offset = 0, duration = 30): TimelineAudioClip {
    return {
      source: { id: 'source', path: `/${id}.flac`, channels: 2, sampleRate: 48000, duration: 60, role: 'stem', name: id },
      clip: { id, assetId: 'source', start, offset, duration, volume: 1, fadeIn: 0, fadeOut: 0, muted: false, locked: false },
      output: { id } as unknown as AudioNode,
    }
  }
  return { transport, context, nodes, gains, errors, buffering, ended, entry }
}

test('unequal decode delays still start all tracks at one exact audio timestamp', async t => {
  const slow = gate<AudioBuffer>()
  const env = fixture(t, async source => source.path === '/slow.flac' ? slow.promise : buffer())
  const starting = env.transport.start([env.entry('fast'), env.entry('slow')], 0, 30)
  await flush()
  assert.equal(env.nodes.length, 0)
  env.context.currentTime = 5
  slow.resolve(buffer())
  assert.equal(await starting, true)
  const starts = env.nodes.map(node => node.starts[0][0])
  assert.deepEqual(starts, [5.06, 5.06])
  env.context.currentTime = 7.06
  assert.ok(Math.abs(env.transport.currentTime - 2) < 1e-8)
})

test('a later clip is scheduled at its project boundary and never moved to the present', async t => {
  const env = fixture(t)
  const later = env.entry('later', 2.5, 7, 1.25)
  await env.transport.start([env.entry('first', 0, 0, 1), later], 0, 10)
  const node = env.nodes.find(node => node.destination.output.id === 'later')
  assert.deepEqual(node.starts[0], [2.56, 3, 1])
  const tail = env.nodes.filter(node => node.destination.output.id === 'later')[1]
  assert.deepEqual(tail.starts[0], [3.56, 0, 0.25])
  // A delayed UI frame cannot alter already scheduled audio starts.
  env.context.currentTime = 3
  assert.equal(node.starts[0][0], 2.56)
  assert.ok(Math.abs(env.transport.currentTime - 2.94) < 1e-8)
})

test('chunks join at exact timestamps and trimmed clips stop at their own end', async t => {
  const env = fixture(t)
  await env.transport.start([env.entry('trimmed', 0, 3.5, 2)], 0, 20)
  assert.deepEqual(env.nodes.map(node => node.starts[0]), [[0.06, 3.5, 0.5], [0.56, 0, 1.5]])
  assert.ok(Math.abs(env.nodes[1].starts[0][0] + env.nodes[1].starts[0][2] - 2.06) < 1e-8)
})

test('seeking into a gap preserves future clip starts and does not preload the whole song', async t => {
  const requested: number[] = []
  const env = fixture(t, async (_, index) => { requested.push(index); return buffer() })
  await env.transport.start([env.entry('later', 7, 40, 10)], 5, 20)
  const first = env.nodes[0]
  assert.ok(Math.abs(first.starts[0][0] - 2.06) < 1e-8)
  assert.equal(first.starts[0][1], 0)
  assert.ok(requested.every(index => index >= 10 && index <= 11))
  assert.equal(env.transport.currentTime, 5)
})

test('pause and a newer seek cancel old decoding without creating stale audio', async t => {
  const delayed = gate<AudioBuffer>()
  const env = fixture(t, async (_, index) => index === 0 ? delayed.promise : buffer())
  const old = env.transport.start([env.entry()], 0, 30)
  await flush()
  env.transport.pause()
  assert.equal(await old, false)
  await env.transport.start([env.entry()], 12, 30)
  delayed.resolve(buffer())
  await flush()
  assert.ok(env.nodes.every(node => node.starts[0][1] === 0))
  assert.ok(env.transport.currentTime >= 12)
  env.transport.pause()
  assert.ok(env.nodes.every(node => node.stopped))
})

test('a missed prefetch deadline holds every track at one boundary instead of skipping audio', async t => {
  const delayed = gate<AudioBuffer>()
  const env = fixture(t, async (_, index) => index === 1 ? delayed.promise : buffer())
  await env.transport.start([env.entry('one'), env.entry('two')], 0, 30)
  await flush()
  env.context.currentTime = 8
  assert.equal(env.transport.currentTime, 4)
  delayed.resolve(buffer())
  await flush()
  assert.deepEqual(env.buffering, [true, false])
  assert.equal(env.transport.currentTime, 4)
  const resumed = env.nodes.filter(node => !node.stopped)
  assert.ok(resumed.every(node => node.starts[0][0] >= 8.06))
  assert.equal(resumed[0].starts[0][0], resumed[1].starts[0][0])
})

test('clip fades and gain are scheduled on audio time, including future clips', async t => {
  const env = fixture(t)
  const clip = env.entry('fade', 1, 0, 2)
  Object.assign(clip.clip, { volume: 0.5, fadeIn: 0.5, fadeOut: 0.5 })
  await env.transport.start([clip], 0, 10)
  assert.deepEqual(env.gains[0].events, [[0, 1.06], [0.5, 1.56], [0.5, 2.56], [0, 3.06]])
  const starts = env.nodes.map(node => node.starts)
  clip.clip.volume = 0.25
  env.transport.updateMix([clip])
  assert.deepEqual(env.nodes.map(node => node.starts), starts)
  assert.equal(env.gains[0].events.at(-3)?.[0], 0.25)
})

test('shared sources reuse decoded chunks and decoder failures cancel startup', async t => {
  let reads = 0
  const env = fixture(t, async () => { reads++; return buffer() })
  const first = env.entry('one'), second = env.entry('two')
  second.source = first.source
  await env.transport.start([first, second], 0, 3)
  assert.equal(reads, 1)
  env.transport.pause()
  await env.transport.start([first, second], 0, 3)
  assert.equal(reads, 1)
  const failed = fixture(t, async () => { throw new Error('Decode failed') })
  await assert.rejects(failed.transport.start([failed.entry()], 0, 3), /Decode failed/)
  assert.equal(failed.nodes.length, 0)
})

test('replacing clip mix settings also updates chunks prepared after the edit', async t => {
  const env = fixture(t)
  const original = env.entry()
  await env.transport.start([original], 0, 30)
  await flush()
  const replacement = { ...original, clip: { ...original.clip, volume: 0.5, fadeIn: 12 } }
  env.transport.updateMix([replacement])
  await env.transport.pump()
  const future = env.nodes.find(node => Math.abs(node.starts[0][0] - 8.06) < 1e-8)
  assert.ok(future)
  assert.ok(Math.abs(future.destination.events[0][0] - 0.5 * 8 / 12) < 1e-8)
})

test('a late refill uses its prepared group even when cache entries have been evicted', async t => {
  const delayed = gate<AudioBuffer>()
  const reads = new Map<string, number>()
  const large = { duration: 4, length: 192000 * 4, numberOfChannels: 2 } as AudioBuffer
  const env = fixture(t, async (source, index) => {
    const key = `${source.path}:${index}`
    reads.set(key, (reads.get(key) || 0) + 1)
    if (index === 1 && reads.get(key)! > 1) throw new Error('Unexpected second decode')
    return index === 1 ? delayed.promise : large
  })
  const entries = Array.from({ length: 24 }, (_, index) => env.entry(`track-${index}`))
  await env.transport.start(entries, 0, 30)
  await flush()
  env.context.currentTime = 8
  delayed.resolve(large)
  await flush()
  assert.deepEqual(env.errors, [])
  assert.deepEqual(env.buffering, [true, false])
  assert.equal(env.transport.isPlaying, true)
  assert.equal(env.transport.currentTime, 4)
  assert.ok([...reads].filter(([key]) => key.endsWith(':1')).every(([, count]) => count === 1))
})

test('overlapping fades multiply like export and can be edited during playback', async t => {
  const env = fixture(t)
  const entry = env.entry('overlap', 1, 0, 2)
  Object.assign(entry.clip, { volume: 0.5, fadeIn: 2, fadeOut: 2 })
  await env.transport.start([entry], 0, 4)
  const gain = env.gains[0]
  assert.equal(gain.curves[0].values[128], 0.125)
  env.context.currentTime = 2
  const replacement = { ...entry, clip: { ...entry.clip, volume: 0.25 } }
  env.transport.updateMix([replacement])
  assert.equal(gain.curves.length, 2)
  assert.deepEqual(gain.cancelled, [0, 0])
  assert.ok(gain.curves[1].when >= 2 - 1e-8)
})

test('cancelled queued reads do not delay the newest seek or consume decoder slots', async t => {
  const finishes: Array<() => void> = []
  const invoked: number[] = []
  const read = setup(t, async (_, args) => {
    invoked.push(args.index)
    return new Promise(resolve => finishes.push(() => resolve(new Float32Array([1, 1]).buffer)))
  })
  const controller = new AbortController()
  const obsolete = Array.from({ length: 6 }, (_, index) => read(source, index, controller.signal))
  const settled = Promise.allSettled(obsolete)
  await flush()
  assert.deepEqual(invoked, [0, 1, 2, 3])
  controller.abort()
  const latest = read(source, 9)
  for (const finish of finishes.slice()) finish()
  await flush()
  assert.deepEqual(invoked, [0, 1, 2, 3, 9])
  finishes.at(-1)!()
  await latest
  assert.ok((await settled).every(result => result.status === 'rejected' && result.reason.name === 'AbortError'))
})

test('a new seek cancels obsolete chunks but retains the overlapping initial window', async t => {
  const requests: Array<{ index: number; signal?: AbortSignal }> = []
  const delayed = gate<AudioBuffer>()
  const env = fixture(t, async (_, index, signal?: AbortSignal) => {
    requests.push({ index, signal }); return delayed.promise
  })
  const old = env.transport.start([env.entry()], 0, 30)
  await flush()
  const newest = env.transport.start([env.entry()], 12, 30)
  await flush()
  assert.equal(requests[0].signal?.aborted, true)
  assert.equal(await old, false)
  const sameWindow = env.transport.start([env.entry()], 13, 30)
  await flush()
  assert.equal(requests.find(request => request.index === 3)?.signal?.aborted, false)
  assert.equal(requests.filter(request => request.index === 3).length, 1)
  delayed.resolve(buffer())
  assert.equal(await newest, false)
  assert.equal(await sameWindow, true)
})

test('partial node startup failure stops the group and preserves the original error', async t => {
  const env = fixture(t)
  const create = env.context.createBufferSource.bind(env.context)
  let count = 0
  env.context.createBufferSource = () => {
    const node = create()
    if (++count === 2) {
      node.start = () => { throw new Error('Audio output closed') }
      node.stop = () => { throw new Error('Cannot stop a node that never started') }
    }
    return node
  }
  await assert.rejects(env.transport.start([env.entry('one'), env.entry('two')], 0, 4), /Audio output closed/)
  assert.equal(env.transport.isPlaying, false)
  assert.equal(env.nodes[0].stopped, true)
})

test('buffer exhaustion reports waiting before decoding finishes and holds the shared position', async t => {
  const delayed = gate<AudioBuffer>()
  const env = fixture(t, async (_, index) => index === 1 ? delayed.promise : buffer())
  await env.transport.start([env.entry()], 0, 30)
  await flush()
  env.context.currentTime = 5
  await env.transport.pump()
  assert.deepEqual(env.buffering, [true])
  assert.equal(env.transport.isPlaying, false)
  assert.equal(env.transport.currentTime, 4)
  env.context.currentTime = 10
  await env.transport.pump()
  assert.deepEqual(env.buffering, [true])
  delayed.resolve(buffer())
  await flush()
  assert.deepEqual(env.buffering, [true, false])
  assert.equal(env.transport.isPlaying, true)
  assert.equal(env.transport.currentTime, 4)
})

test('source metadata hydration cannot change an in-flight window cache key', async t => {
  const delayed = gate<AudioBuffer>()
  const sources: EditorSource[] = []
  const env = fixture(t, async source => { sources.push(source); return delayed.promise })
  const entry = env.entry()
  const starting = env.transport.start([entry], 0, 4)
  await flush()
  entry.source.sampleRate = 44100
  entry.source.channels = 1
  assert.equal(sources[0].sampleRate, 48000)
  assert.equal(sources[0].channels, 2)
  delayed.resolve(buffer())
  assert.equal(await starting, true)
  assert.equal(env.nodes.length, 1)
})

test('a background preload cannot cancel a newer play request waiting for its initial window', async t => {
  const delayed = gate<AudioBuffer>()
  let activeSignal: AbortSignal | undefined
  const env = fixture(t, async (_, index, signal?: AbortSignal) => {
    if (index !== 3) return buffer()
    activeSignal = signal
    return new Promise<AudioBuffer>((resolve, reject) => {
      delayed.promise.then(resolve, reject)
      signal?.addEventListener('abort', () => reject(new DOMException('Cancelled', 'AbortError')), { once: true })
    })
  })
  const entry = env.entry()
  const playing = env.transport.start([entry], 12, 30)
  const result = playing.then(value => ({ value }), error => ({ error }))
  await flush()
  await env.transport.preload([entry], 0, 30)
  assert.equal(activeSignal?.aborted, false)
  delayed.resolve(buffer())
  assert.deepEqual(await result, { value: true })
})

test('an overlapping short fade retains its exact corner instead of smearing over the chunk', async t => {
  const env = fixture(t)
  const entry = env.entry('short-fade', 0, 0, 2)
  Object.assign(entry.clip, { fadeIn: 0.01, fadeOut: 2 })
  await env.transport.start([entry], 0, 2)
  const curves = env.gains[0].curves
  assert.equal(curves.length, 2)
  assert.ok(Math.abs(curves[0].duration - 0.01) < 1e-8)
  assert.ok(Math.abs(curves[1].when - 0.07) < 1e-8)
  assert.ok(Math.abs(curves[0].values.at(-1)! - 0.995) < 1e-7)
  assert.equal(curves[0].values.at(-1), curves[1].values[0])
})

test('failed initial decoding releases foreground priority so preloading can retry', async t => {
  let reads = 0
  const env = fixture(t, async () => {
    if (++reads === 1) throw new Error('Temporary decode failure')
    return buffer()
  })
  const entry = env.entry()
  await assert.rejects(env.transport.start([entry], 0, 4), /Temporary decode failure/)
  await env.transport.preload([entry], 0, 4)
  assert.equal(reads, 2)
  assert.equal(await env.transport.start([entry], 0, 4), true)
  assert.equal(reads, 2)
})

test('project completion releases the audio transport without waiting for a UI frame', async t => {
  const env = fixture(t)
  await env.transport.start([env.entry('short', 0, 0, 1)], 0, 1)
  env.context.currentTime = 2
  await env.transport.pump()
  assert.equal(env.transport.isPlaying, false)
  assert.equal(env.transport.currentTime, 1)
  assert.deepEqual(env.ended, [1])
  assert.ok(env.nodes.every(node => node.stopped))
  await env.transport.pump()
  assert.deepEqual(env.ended, [1])
})

test('duration-only edits keep scheduled audio and advance through an extended silent tail', async t => {
  const env = fixture(t)
  await env.transport.start([env.entry('audible', 0, 0, 1)], 0, 2)
  const [node] = env.nodes
  env.context.currentTime = 0.56
  env.transport.setDuration(1.5)
  assert.ok(Math.abs(env.transport.currentTime - 0.5) < 1e-8)
  assert.equal(node.stopped, false)
  assert.equal(env.nodes.length, 1)

  env.transport.setDuration(3)
  env.context.currentTime = 2.56
  await env.transport.pump()
  assert.ok(Math.abs(env.transport.currentTime - 2.5) < 1e-8)
  assert.deepEqual(env.buffering, [])
  assert.equal(node.stopped, false)
  env.transport.setDuration(2.25)
  await env.transport.pump()
  assert.deepEqual(env.ended, [2.25])
})

test('duration edits during a pending start apply when the audio becomes ready', async t => {
  const delayed = gate<AudioBuffer>()
  const env = fixture(t, async () => delayed.promise)
  const starting = env.transport.start([env.entry('audible', 0, 0, 1)], 0, 2)
  env.transport.setDuration(4)
  delayed.resolve(buffer())
  assert.equal(await starting, true)
  await env.transport.pump()
  env.context.currentTime = 3.06
  assert.ok(Math.abs(env.transport.currentTime - 3) < 1e-8)
  assert.deepEqual(env.ended, [])
})

test('an overestimated source tail does not interrupt other tracks or get decoded repeatedly', async t => {
  const reads: number[] = []
  const env = fixture(t, async (source, index) => {
    if (source.path !== '/estimated.flac') return buffer()
    reads.push(index)
    return index < 3 ? buffer() : null
  })
  const tail = env.entry('estimated', 0, 0, 12.042449)
  tail.source.duration = tail.clip.duration
  const other = env.entry('other')
  assert.equal(await env.transport.start([tail, other], 0, 30), true)
  await flush()
  await env.transport.pump()
  env.context.currentTime = 4.2
  await env.transport.pump()
  assert.deepEqual(reads, [0, 1, 2, 3])
  assert.equal(env.transport.isPlaying, true)
  assert.deepEqual(env.errors, [])
  env.context.currentTime = 12.2
  await env.transport.pump()
  assert.ok(env.nodes.some(node => node.destination.output.id === 'other' && node.starts[0][0] >= 16))
  assert.equal(env.nodes.filter(node => node.destination.output.id === 'estimated').length, 3)
  // Replay from the metadata-only tail. Known EOF survives pause and preloading.
  env.transport.pause()
  await env.transport.preload([tail, other], 12.01, 30)
  assert.equal(await env.transport.start([tail, other], 12.01, 30), true)
  assert.deepEqual(reads, [0, 1, 2, 3])
  assert.deepEqual(env.errors, [])
})

test('a short final block bounds future reads at its exact decoded end', async t => {
  const reads: number[] = []
  const env = fixture(t, async (_, index) => {
    reads.push(index)
    if (index > 3) throw new Error('Read beyond known EOF')
    return buffer(index === 3 ? 3.998548752834466 : 4)
  })
  const entry = env.entry('short-tail', 0, 0, 16.058604)
  entry.source.duration = entry.clip.duration
  await env.transport.start([entry], 0, entry.clip.duration)
  await flush()
  await env.transport.pump()
  env.context.currentTime = 4.2
  await env.transport.pump()
  await env.transport.pump()
  assert.deepEqual(reads, [0, 1, 2, 3])
  assert.deepEqual(env.errors, [])
  assert.equal(env.transport.isPlaying, true)
  const last = env.nodes.at(-1).starts[0]
  assert.ok(Math.abs(last[0] + last[2] - (12 + buffer(3.998548752834466).duration + 0.06)) < 1e-8)
  env.context.currentTime = entry.clip.duration + 0.06 + 1 / env.context.sampleRate
  await env.transport.pump()
  assert.equal(env.ended.length, 1)
  assert.ok(Math.abs(env.ended[0] - entry.clip.duration) <= 0.5 / env.context.sampleRate)
})

test('seeking directly into an unknown metadata-only tail completes without an audio error', async t => {
  const env = fixture(t, async (_, index) => index >= 3 ? null : buffer())
  const entry = env.entry('tail', 0, 0, 12.042449)
  entry.source.duration = entry.clip.duration
  assert.equal(await env.transport.start([entry], 12.01, entry.clip.duration), true)
  assert.equal(env.nodes.length, 0)
  env.context.currentTime = 1
  await env.transport.pump()
  assert.deepEqual(env.errors, [])
  assert.equal(env.ended.length, 1)
  assert.ok(Math.abs(env.ended[0] - entry.clip.duration) <= 0.5 / env.context.sampleRate)
})

test('cancelled decoding cannot install a stale source end in a replacement playback', async t => {
  const delayed = gate<AudioBuffer>()
  let stale = true
  const env = fixture(t, async (_, index) => index === 3 && stale ? delayed.promise : buffer())
  const entry = env.entry()
  const obsolete = env.transport.start([entry], 12, 30)
  await flush()
  assert.equal(await env.transport.start([entry], 0, 30), true)
  assert.equal(await obsolete, false)
  stale = false
  delayed.resolve(buffer(0.1))
  await flush()
  await env.transport.pump()
  env.context.currentTime = 4.2
  await env.transport.pump()
  const replacement = env.nodes.find(node => Math.abs(node.starts[0][0] - 12.06) < 1e-8)
  assert.ok(replacement)
  assert.equal(replacement.starts[0][2], 4)
  assert.deepEqual(env.errors, [])
})

for (const rate of [44100, 48000]) {
  test(`fractional clip and seek positions join on the ${rate} Hz output sample grid`, async t => {
    const env = fixture(t, async () => buffer(4, rate))
    env.context.sampleRate = rate
    const entry = env.entry('fractional', 0.321098, 1.234567, 9.123456)
    await env.transport.start([entry], 0.654321, 10)
    await flush()
    await env.transport.pump()
    const starts = env.nodes.map(node => node.starts[0])
    assert.ok(starts.length >= 3)
    for (const start of starts) {
      for (const seconds of start) assert.ok(Math.abs(seconds * rate - Math.round(seconds * rate)) < 1e-8)
    }
    for (let index = 1; index < starts.length; index++) {
      assert.equal(Math.round((starts[index - 1][0] + starts[index - 1][2]) * rate), Math.round(starts[index][0] * rate))
    }
  })
}
