import assert from 'node:assert/strict'
import test from 'node:test'
import { buildModelCategoryOptionsFromModels, getModelCategoryLabel, hasModelCategoryOption, matchesModelCategory } from '../src/utils/modelCategory.ts'

const vocal = { primaryCategory: 'vocal', primaryCategoryCn: '人声', secondaryCategory: 'duet', secondaryCategoryCn: '双人/二重唱分离' }
const labels = '全部'

test('category breadcrumbs show both levels without splitting slashes inside names', () => {
  assert.equal(getModelCategoryLabel(vocal, 'zh-CN'), '人声 › 双人/二重唱分离')
  assert.equal(getModelCategoryLabel(vocal, 'en'), 'vocal › duet')
})

test('duplicate category labels and empty secondary IDs do not duplicate the parent', () => {
  const leaf = { primaryCategory: 'instrumental', primaryCategoryCn: '伴奏/无人声提取', secondaryCategoryCn: '伴奏/无人声提取' }
  assert.equal(getModelCategoryLabel(leaf, 'zh-CN'), '伴奏/无人声提取')
  assert.equal(getModelCategoryLabel(leaf, 'en'), 'instrumental')
  const options = buildModelCategoryOptionsFromModels([leaf], 'zh-CN', labels)
  assert.deepEqual(options, [{ label: '全部', value: '' }, { label: '伴奏/无人声提取', value: 'instrumental' }])
})

test('legacy and partial metadata keep a readable fallback in either language', () => {
  assert.equal(getModelCategoryLabel({ category: 'legacy/path', categoryCn: '旧分类' }, 'zh-CN'), '旧分类')
  assert.equal(getModelCategoryLabel({ primaryCategoryCn: '人声' }, 'en'), '人声')
  assert.equal(getModelCategoryLabel({ primaryCategory: 'vocal', secondaryCategory: 'duet' }, 'zh-CN'), 'vocal › duet')
  assert.equal(getModelCategoryLabel(null, 'en', 'Unknown'), 'Unknown')
})

test('grouped options let users select the whole parent or a real child with stable IDs', () => {
  const other = { ...vocal, secondaryCategory: 'extraction', secondaryCategoryCn: '人声提取' }
  const rows = [vocal, other, { ...vocal }, { primaryCategory: 'vocal', primaryCategoryCn: '人声', secondaryCategoryCn: '无独立标识' }]
  const options = buildModelCategoryOptionsFromModels(rows, 'zh-CN', labels)
  const group = options[1]
  assert.ok(group.children)
  assert.equal(group.label, '人声')
  assert.equal(group.value, 'vocal')
  assert.equal(group.children.some(child => child.value === 'vocal'), false)
  assert.equal(group.children.length, 2)
  assert.equal(rows.filter(row => matchesModelCategory(row, 'vocal')).length, 4)
  assert.equal(rows.filter(row => matchesModelCategory(row, 'vocal/duet')).length, 2)
  for (const child of group.children) assert.ok(rows.some(row => matchesModelCategory(row, child.value)))
  assert.equal(hasModelCategoryOption(options, 'vocal/duet'), true)
  assert.equal(hasModelCategoryOption(options, 'removed/child'), false)
})

test('old category filters still match and unrelated child paths cannot leak across parents', () => {
  assert.equal(matchesModelCategory(vocal, 'DUET'), true)
  assert.equal(matchesModelCategory(vocal, ''), true)
  assert.equal(matchesModelCategory(vocal, 'other/duet'), false)
  assert.equal(matchesModelCategory({ category: 'old/leaf' }, 'old/leaf'), true)
  assert.equal(matchesModelCategory({ category: 'old/leaf' }, 'old'), true)
})

test('path-only cached models share parents with fresh metadata', () => {
  const legacy = { category: 'vocal/duet', categoryCn: '人声 / 双人/二重唱分离' }
  const options = buildModelCategoryOptionsFromModels([legacy, vocal], 'zh-CN', labels)
  assert.equal(options.length, 2)
  const group = options[1]
  assert.ok(group.children)
  assert.deepEqual(group.children, [{ label: '双人/二重唱分离', value: 'vocal/duet' }])
  assert.equal(getModelCategoryLabel(legacy, 'zh-CN'), '人声 › 双人/二重唱分离')
  assert.equal(matchesModelCategory(legacy, 'vocal'), true)
})

test('task category order keeps vocals, instrumental and karaoke adjacent in both locales', () => {
  const rows = ['unknown', 'karaoke', 'music_stems', 'instrumental', 'vocal'].map(primaryCategory => ({ primaryCategory }))
  for (const locale of ['zh-CN', 'en']) {
    const options = buildModelCategoryOptionsFromModels(rows, locale, labels)
    assert.deepEqual(options.map(option => option.value), ['', 'vocal', 'instrumental', 'karaoke', 'music_stems', 'unknown'])
  }
})
