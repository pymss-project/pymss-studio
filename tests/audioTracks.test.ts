import assert from 'node:assert/strict'
import test from 'node:test'
import { selectTrackLanguage, trackLanguageStates } from '../src/utils/audioTracks.ts'

// Episode 2 lists English first, so language (not track number) must drive the batch control.
const tracks = {
  'ep1.mkv': [{ index: 1, language: 'jpn' }, { index: 2, language: 'eng' }],
  'ep2.mkv': [{ index: 1, language: 'eng' }, { index: 2, language: 'jpn' }, { index: 3, language: 'und' }],
}
const paths = ['ep1.mkv', 'song.wav', 'ep2.mkv']

test('batch languages reflect every file and skip untagged tracks', () => {
  const states = trackLanguageStates(paths, tracks, { 'ep1.mkv': [1, 2], 'ep2.mkv': [1, 3] })
  assert.deepEqual(states, [
    { language: 'jpn', checked: false, indeterminate: true },
    { language: 'eng', checked: true, indeterminate: false },
  ])
  assert.deepEqual(trackLanguageStates(['ep1.mkv'], tracks, {}), [])
})

test('toggling a language changes only that language in each file and keeps track order', () => {
  const selection = { 'ep1.mkv': [1, 2], 'ep2.mkv': [1, 2, 3] }
  const englishOff = selectTrackLanguage(paths, tracks, selection, 'eng', false)
  assert.deepEqual(englishOff, { 'ep1.mkv': [1], 'ep2.mkv': [2, 3] })
  assert.deepEqual(selectTrackLanguage(paths, tracks, englishOff, 'eng', true), selection)
  assert.deepEqual(selection, { 'ep1.mkv': [1, 2], 'ep2.mkv': [1, 2, 3] })
})
