import assert from 'node:assert/strict'
import test from 'node:test'
import {
  ASR_LANGUAGE_KEYS,
  ASR_PRESETS,
  DEFAULT_ASR_PRESET,
  findAsrPreset,
} from '../../../plugins/asr/ui/src/profiles.ts'

test('every ASR preset has a valid default language and unique identifiers', () => {
  assert.equal(new Set(ASR_PRESETS.map(preset => preset.id)).size, ASR_PRESETS.length)
  assert.equal(new Set(ASR_PRESETS.map(preset => preset.modelId)).size, ASR_PRESETS.length)
  for (const preset of ASR_PRESETS) {
    assert.ok(preset.languages.includes(preset.defaultLanguage), `${preset.id} has an invalid default language`)
    assert.ok(preset.languages.every(language => Boolean(ASR_LANGUAGE_KEYS[language])))
  }
})

test('the default ASR preset remains the timestamp-capable Chinese profile', () => {
  const preset = findAsrPreset(DEFAULT_ASR_PRESET)
  assert.equal(preset?.id, 'paraformer-zh')
  assert.equal(preset?.supportsTimestamps, true)
  assert.deepEqual(preset?.languages, ['zh'])
})

test('large multilingual profile exposes all documented languages without promising timestamps', () => {
  const preset = findAsrPreset('fun-asr-mlt-nano')
  assert.equal(preset?.languages.length, 32)
  assert.equal(preset?.supportsTimestamps, false)
  assert.equal(preset?.languages.includes('pt'), true)
})
