import test from 'node:test'
import assert from 'node:assert/strict'

import { normalizeStemOutputs, stemFromOutputPath } from '../src/utils/stemOutputs.ts'

test('normalizes advanced workflow output metadata to the shared stem contract', () => {
  assert.deepEqual(
    normalizeStemOutputs([
      { path: 'D:/results/song/song_vocals.wav', name: 'song_vocals.wav' },
      { path: 'D:/results/song/song_instrumental.wav' },
    ], 'D:/audio/song.wav'),
    [
      { stem: 'vocals', path: 'D:/results/song/song_vocals.wav' },
      { stem: 'instrumental', path: 'D:/results/song/song_instrumental.wav' },
    ],
  )
})

test('preserves explicit stem metadata and drops malformed entries', () => {
  assert.deepEqual(
    normalizeStemOutputs([
      { stem: 'Lead Vocals', path: 'D:/results/lead.wav', name: 'ignored.wav' },
      { name: 'missing-path.wav' },
      null,
    ], 'D:/audio/song.wav'),
    [{ stem: 'Lead Vocals', path: 'D:/results/lead.wav' }],
  )
})

test('keeps custom workflow filenames when no input prefix is present', () => {
  assert.equal(stemFromOutputPath('D:/results/custom_mix.wav', 'D:/audio/song.wav'), 'custom_mix')
})

test('generic command file metadata survives normalization and history restoration', () => {
  const outputs = normalizeStemOutputs([{ path: '/results/normalized.txt', kind: 'file', mediaType: 'text/plain', nodeId: 'convert' }])
  assert.deepEqual(outputs, [{ path: '/results/normalized.txt', stem: 'normalized', kind: 'file', mediaType: 'text/plain', nodeId: 'convert' }])
  assert.deepEqual(normalizeStemOutputs(JSON.parse(JSON.stringify(outputs))), outputs)
})

test('native POSIX stems preserve literal backslashes while Windows input hints stay compatible', () => {
  assert.equal(stemFromOutputPath('/results/custom\\mix.wav', '/audio/song.wav'), 'custom\\mix')
  assert.equal(stemFromOutputPath('/results/AC\\DC_vocals.wav', '/audio/AC\\DC.wav'), 'vocals')
  assert.deepEqual(normalizeStemOutputs([{ path: 'song_vocals.wav' }], 'C:\\audio\\song.wav'), [
    { path: 'song_vocals.wav', stem: 'vocals' },
  ])
})
