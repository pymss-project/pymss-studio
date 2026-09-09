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
