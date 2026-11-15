import assert from 'node:assert/strict'
import test from 'node:test'
import { audioTrackLanguageLabel, selectTrackLanguage, trackLanguageStates } from '../src/utils/audioTracks.ts'

// Episode 2 lists English first, so language (not track number) must drive the batch control.
const tracks = {
  'ep1.mkv': [{ index: 1, language: 'jpn' }, { index: 2, language: 'eng' }],
  'ep2.mkv': [{ index: 1, language: 'eng' }, { index: 2, language: 'jpn' }, { index: 3, language: 'und' }],
}
const paths = ['ep1.mkv', 'song.wav', 'ep2.mkv']

test('audio track language codes use the interface locale, including ISO 639-2 aliases', () => {
  assert.equal(audioTrackLanguageLabel('zho', 'zh-CN', '未标注语言'), '中文')
  assert.equal(audioTrackLanguageLabel('chi', 'zh-CN', '未标注语言'), '中文')
  assert.equal(audioTrackLanguageLabel('eng', 'zh-CN', '未标注语言'), '英语')
  assert.equal(audioTrackLanguageLabel('jpn', 'en', 'Untagged language'), 'Japanese')
})

test('missing language tags are explicit and unknown tags retain their original code', () => {
  for (const code of ['', 'und', ' UND ']) {
    assert.equal(audioTrackLanguageLabel(code, 'zh-CN', '未标注语言'), '未标注语言')
  }
  assert.equal(audioTrackLanguageLabel('xyz', 'zh-CN', '未标注语言'), 'xyz')
  assert.equal(audioTrackLanguageLabel('invalid_code', 'en', 'Untagged language'), 'invalid_code')
})

test('batch languages reflect every file and skip untagged tracks', () => {
  const states = trackLanguageStates(paths, tracks, { 'ep1.mkv': [1, 2], 'ep2.mkv': [1, 3] })
  assert.deepEqual(states, [
    { language: 'ja', checked: false, indeterminate: true },
    { language: 'en', checked: true, indeterminate: false },
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

test('language aliases form one batch control and update every matching file', () => {
  const files = ['a.mkv', 'b.mkv', 'c.mkv']
  const tracks = {
    'a.mkv': [{ index: 1, language: 'zho' }, { index: 2, language: 'eng' }],
    'b.mkv': [{ index: 1, language: 'chi' }, { index: 2, language: 'en' }],
    'c.mkv': [{ index: 1, language: ' ZH ' }, { index: 2, language: 'und' }],
  }
  const selection = { 'a.mkv': [1, 2], 'b.mkv': [2], 'c.mkv': [1, 2] }
  assert.deepEqual(trackLanguageStates(files, tracks, selection), [
    { language: 'zh', checked: false, indeterminate: true },
    { language: 'en', checked: true, indeterminate: false },
  ])
  const allChinese = selectTrackLanguage(files, tracks, selection, 'chi', true)
  assert.deepEqual(allChinese, { 'a.mkv': [1, 2], 'b.mkv': [1, 2], 'c.mkv': [1, 2] })
  assert.deepEqual(selectTrackLanguage(files, tracks, allChinese, 'zh', false), {
    'a.mkv': [2], 'b.mkv': [2], 'c.mkv': [2],
  })
})

test('language grouping retains regional distinctions and skips untagged variants', () => {
  const files = ['a.mkv', 'b.mkv']
  const tracks = {
    'a.mkv': [{ index: 1, language: 'zh-Hant' }, { index: 2, language: ' UND ' }],
    'b.mkv': [{ index: 1, language: 'zho' }, { index: 2, language: '' }],
  }
  assert.deepEqual(trackLanguageStates(files, tracks, {}).map(item => item.language), ['zh-Hant', 'zh'])
  assert.deepEqual(selectTrackLanguage(files, tracks, {}, 'zh', true), { 'b.mkv': [1] })
})
