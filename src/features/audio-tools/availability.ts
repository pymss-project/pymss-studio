import type { AudioToolDefinition, AudioToolKey } from './types'

// ASR was historically hidden on macOS because FunASR's torch stack had
// Apple-Silicon compatibility gaps. With the funasr-mlx plugin backend
// available (and paraformer-zh working on CPU), the page is now usable on
// macOS — keep the set for any future platform-specific tool.
export const MACOS_HIDDEN_AUDIO_TOOL_IDS: ReadonlySet<AudioToolKey> = new Set()

export function isAudioToolAvailable(tool: AudioToolDefinition, isMacOS: boolean) {
  if (tool.hidden) return false
  return !isMacOS || !MACOS_HIDDEN_AUDIO_TOOL_IDS.has(tool.id)
}
