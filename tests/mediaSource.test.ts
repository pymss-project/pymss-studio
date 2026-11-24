import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'
import { loadPreviewMediaSource, releasePreviewMediaSource } from '../src/utils/mediaSource.ts'

function useLinuxPreview(t: TestContext) {
  const windowDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { __TAURI_INTERNALS__: { convertFileSrc: (path: string) => `asset://localhost/${path}` } },
  })
  Object.defineProperty(navigator, 'userAgent', { configurable: true, value: 'Linux' })
  t.after(() => {
    if (windowDescriptor) Object.defineProperty(globalThis, 'window', windowDescriptor)
    else Reflect.deleteProperty(globalThis, 'window')
    Reflect.deleteProperty(navigator, 'userAgent')
  })
}

test('concurrent preview requests share one fetch and release one Blob URL', async (t) => {
  useLinuxPreview(t)
  const fetch = t.mock.method(globalThis, 'fetch', async () => new Response('audio'))
  const create = t.mock.method(URL, 'createObjectURL', () => 'blob:preview')
  const revoke = t.mock.method(URL, 'revokeObjectURL', () => {})
  const audio = { src: '', load() {} } as HTMLAudioElement

  assert.deepEqual(await Promise.all([
    loadPreviewMediaSource(audio, 'audio.wav'),
    loadPreviewMediaSource(audio, 'audio.wav'),
  ]), [true, true])
  assert.equal(fetch.mock.callCount(), 1)
  assert.equal(create.mock.callCount(), 1)
  assert.equal(audio.src, 'blob:preview')
  releasePreviewMediaSource(audio)
  releasePreviewMediaSource(audio)
  assert.deepEqual(revoke.mock.calls.map(call => call.arguments), [['blob:preview']])
})

test('a released preview aborts its fetch and discards a late response', async (t) => {
  useLinuxPreview(t)
  let resolve!: (response: Response) => void
  let signal: AbortSignal | null | undefined
  t.mock.method(globalThis, 'fetch', (_url, options) => {
    signal = options?.signal
    return new Promise<Response>(complete => { resolve = complete })
  })
  const create = t.mock.method(URL, 'createObjectURL', () => 'blob:stale')
  const audio = { src: '', load() {} } as HTMLAudioElement

  const ready = loadPreviewMediaSource(audio, 'audio.wav')
  releasePreviewMediaSource(audio)
  assert.equal(signal?.aborted, true)
  resolve(new Response('audio'))
  assert.equal(await ready, false)
  assert.equal(create.mock.callCount(), 0)
  assert.equal(audio.src, '')
})
