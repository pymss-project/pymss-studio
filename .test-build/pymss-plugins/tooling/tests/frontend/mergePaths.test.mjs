import assert from 'node:assert/strict'
import test from 'node:test'
import { sameAudioDirectory } from '../../frontend/src/features/audio-tools/paths.ts'

test('merge directory comparisons retain POSIX case and normalize Windows paths', () => {
  for (const [first, second] of [['C:\\Music\\', 'c:/music'], ['C:/Music/.', 'C:/Music'], ['\\\\server\\audio\\', '//server/audio'], ['/audio/', '/audio/.'], ['/', '/']]) {
    assert.equal(sameAudioDirectory(first, second), true)
  }
  for (const [first, second] of [['/Audio', '/audio'], ['', ''], ['/input', '/output']]) {
    assert.equal(sameAudioDirectory(first, second), false)
  }
})

test('merge directory comparisons preserve literal backslashes in POSIX names', () => {
  assert.equal(sameAudioDirectory('/audio/a\\b', '/audio/a/b'), false)
  assert.equal(sameAudioDirectory('/audio/a\\b', '/audio/A\\b'), false)
  assert.equal(sameAudioDirectory('/audio/C:\\Music', '/audio/c:/music'), false)
  assert.equal(sameAudioDirectory('/audio/a\\b/', '/audio/a\\b/.'), true)
})
