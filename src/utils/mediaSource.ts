import { convertFileSrc } from '@tauri-apps/api/core'

/**
 * WebKitGTK cannot play audio or video from Tauri's custom `asset:` protocol
 * (https://github.com/tauri-apps/tauri/issues/8654), so on Linux the asset is
 * fetched through the protocol handler and played from a blob URL instead.
 */
export function needsBlobMediaSource() {
  return typeof navigator !== 'undefined'
    && /Linux/i.test(navigator.userAgent)
    && !/Android/i.test(navigator.userAgent)
}

type PreviewSource = {
  controller: AbortController
  ready: Promise<boolean>
  url?: string
}

const previewSources = new WeakMap<HTMLAudioElement, PreviewSource>()

export async function loadPreviewMediaSource(audio: HTMLAudioElement, path: string) {
  if (audio.src) return true
  const pending = previewSources.get(audio)
  if (pending) return pending.ready
  const assetUrl = convertFileSrc(path)
  if (!needsBlobMediaSource()) {
    audio.src = assetUrl
    audio.load()
    return true
  }

  const controller = new AbortController()
  const ready = fetch(assetUrl, { signal: controller.signal }).then(async (response) => {
    if (!response.ok) throw new Error(`Audio request failed: ${response.status}`)
    const blob = await response.blob()
    if (controller.signal.aborted) return false
    source.url = URL.createObjectURL(blob)
    audio.src = source.url
    audio.load()
    return true
  }).catch((error) => {
    if (controller.signal.aborted) return false
    releasePreviewMediaSource(audio)
    throw error
  })
  const source: PreviewSource = { controller, ready }
  previewSources.set(audio, source)
  return ready
}

export function releasePreviewMediaSource(audio: HTMLAudioElement) {
  const source = previewSources.get(audio)
  if (!source) return
  previewSources.delete(audio)
  source.controller.abort()
  if (source.url) URL.revokeObjectURL(source.url)
}
