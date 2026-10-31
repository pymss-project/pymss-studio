import assert from 'node:assert/strict'
import test from 'node:test'
import { measurementRows, spectrumChart, reportText } from '../../../plugins/analysis/ui/src/report.ts'

test('true peak report exposes named measurements with units and bounded decimals', () => {
  assert.deepEqual(measurementRows('pymss.analysis.measure_true_peak', {
    sample_peak_dbfs: -27.467469477728663, true_peak_dbtp: -27.461217507222108,
  }, 'zh-CN'), [
    { key: 'true_peak_dbtp', text: '-27.46', unit: 'dBTP' },
    { key: 'sample_peak_dbfs', text: '-27.47', unit: 'dBFS' },
  ])
})

test('quiet and non-finite measurements retain their meaning without rendering null as zero', () => {
  for (const value of [-Infinity, '-Infinity', '-inf']) assert.equal(measurementRows('pymss.analysis.measure_lufs', { lufs: value }, 'zh-CN')[0].text, '−∞')
  for (const value of [NaN, 'NaN', null]) assert.equal(measurementRows('pymss.analysis.measure_lufs', { lufs: value }, 'en')[0].text, '—')
  assert.equal(measurementRows('pymss.analysis.measure_lufs', { lufs: 'Infinity' }, 'en')[0].text, '+∞')
  assert.equal(reportText({ lufs: -Infinity }), '{\n  "lufs": "-Infinity"\n}')
  assert.equal(measurementRows('pymss.analysis.measure_rms', { rms_dbfs: -160, rms_linear: 1e-8 }, 'en')[1].text, '1E-8')
})

test('unknown or extended reports fall back to raw data instead of dropping fields', () => {
  assert.equal(measurementRows('unrecognized', { lufs: -20 }, 'en'), null)
  assert.equal(measurementRows('pymss.analysis.measure_lufs', { lufs: -20, extra: 1 }, 'en'), null)
  assert.equal(measurementRows('pymss.analysis.measure_lufs', { lufs: 'unavailable' }, 'en'), null)
  assert.equal(measurementRows('pymss.analysis.measure_rms', { rms_dbfs: -20 }, 'en'), null)
})

test('spectrum chart handles flat signals and bounds points while retaining narrow peaks', () => {
  const flat = spectrumChart({ freqs: [0, 10, 20], magnitudes_db: [-240, -240, -240] })
  assert.equal(flat.minimumDb, -240)
  assert.equal(flat.maximumDb, -230)
  assert.equal(flat.points, '50.00,220.00 400.00,220.00 750.00,220.00')
  const magnitudes = Array(10000).fill(-100)
  magnitudes[50] = 0
  const chart = spectrumChart({ freqs: magnitudes.map((_, index) => index), magnitudes_db: magnitudes })
  assert.ok(chart.points.split(' ').length <= 600)
  assert.ok(chart.points.includes(',20.00'))
  for (const value of [
    { freqs: [0, 1], magnitudes_db: [0] },
    { freqs: [0, 0], magnitudes_db: [0, 0] },
    { freqs: [0, 1], magnitudes_db: [0, Infinity] },
    { freqs: [0, 1], magnitudes_db: [0, 'bad'] },
  ]) assert.equal(spectrumChart(value), null)
})
