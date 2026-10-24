import assert from 'node:assert/strict'
import test from 'node:test'
import { isAudioToolAvailable, MACOS_HIDDEN_AUDIO_TOOL_IDS } from '../src/features/audio-tools/availability.ts'
import type { AudioToolDefinition } from '../src/features/audio-tools/types.ts'

function tool(id: AudioToolDefinition['id'], hidden = false) {
  return { id, hidden } as AudioToolDefinition
}

test('ASR is now available on macOS alongside other platforms', () => {
  // Historically hidden on macOS due to FunASR torch gaps; the funasr-mlx
  // plugin backend and CPU-viable paraformer-zh make it usable. The set stays
  // as the mechanism for any future platform-specific tool.
  assert.equal(MACOS_HIDDEN_AUDIO_TOOL_IDS.has('asr'), false)
  assert.equal(isAudioToolAvailable(tool('asr'), true), true)
  assert.equal(isAudioToolAvailable(tool('asr'), false), true)
  assert.equal(isAudioToolAvailable(tool('midi'), true), true)
})

test('globally hidden audio tools stay hidden on every platform', () => {
  assert.equal(isAudioToolAvailable(tool('convert', true), false), false)
  assert.equal(isAudioToolAvailable(tool('convert', true), true), false)
})
