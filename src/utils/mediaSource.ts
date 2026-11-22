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

export async function mediaSourceUrl(path: string) {
  const url = convertFileSrc(path)
  if (!needsBlobMediaSource()) return url
  const response = await fetch(url)
  if (!response.ok) throw new Error(`Audio request failed: ${response.status}`)
  return URL.createObjectURL(await response.blob())
}

export function releaseMediaSourceUrl(url: string) {
  if (url.startsWith('blob:')) URL.revokeObjectURL(url)
}
