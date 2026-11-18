import assert from 'node:assert/strict'
import { describe, test } from 'node:test'
import { buildModelCategoryOptionsFromModels, getModelCategoryLabel, hasModelCategoryOption, matchesModelCategory } from '../src/utils/modelCategory.ts'
import { matchesModelQuery } from '../src/utils/modelSearch.ts'
import { matchesModelSource, retainAvailableModelNames } from '../src/utils/modelSource.ts'
import { compareModelDefaultOrder } from '../src/utils/modelSort.ts'

describe('Model categories', () => {
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
})

describe('Model search', () => {
  const MODEL = {
    name: 'bs_roformer_voc.ckpt',
    aliases: ['voc_hyperace'],
    architecture: 'bs_roformer',
    modelType: 'bs_roformer',
    targetStem: 'vocals',
    configTargetInstrument: 'vocals',
    category: 'vocal/separation',
    categoryCn: '人声 / 分离',
    classificationBasis: 'config_instruments',
  }

  test('an empty query matches everything', () => {
    // Callers pass the raw input value, so a blank box must not filter anything out.
    assert.equal(matchesModelQuery(MODEL, ''), true)
    assert.equal(matchesModelQuery(MODEL, '   '), true)
  })

  test('the note is searchable', () => {
    // The reason this helper exists: the separation page used to search everything except the note,
    // so a model could be found in the library and not when picking one to run.
    assert.equal(matchesModelQuery(MODEL, 'BEST FOR POP', 'best for pop vocals'), true)
    assert.equal(matchesModelQuery(MODEL, 'best for pop'), false)
  })

  test('每个字段都可搜索', () => {
    const cases: Array<[string, string]> = [
      ['name', 'roformer_voc'],
      ['alias', 'hyperace'],
      ['architecture', 'bs_roformer'],
      ['targetStem', 'vocals'],
      ['category', 'separation'],
      ['categoryCn', '人声'],
      ['classificationBasis', 'config_instruments'],
    ]
    for (const [label, query] of cases) {
      assert.equal(matchesModelQuery(MODEL, query), true, `${label} should be searchable`)
    }
  })

  test('a query matching nothing is rejected', () => {
    assert.equal(matchesModelQuery(MODEL, 'drums'), false)
  })

  test('models missing optional fields do not throw', () => {
    // Imported models carry no targetStem or classificationBasis until a config supplies them.
    assert.equal(matchesModelQuery({ name: 'custom' }, 'custom'), true)
    assert.equal(matchesModelQuery({ name: 'custom' }, 'vocals'), false)
    assert.equal(matchesModelQuery({}, 'anything'), false)
  })

  test('a missing model matches nothing but an empty query', () => {
    assert.equal(matchesModelQuery(null, 'x'), false)
    assert.equal(matchesModelQuery(undefined, ''), true)
  })

  test('a null modelType is tolerated', () => {
    assert.equal(matchesModelQuery({ name: 'a', modelType: null }, 'a'), true)
  })
})

describe('Model sources', () => {
  test('the all filter keeps every model', () => {
    assert.equal(matchesModelSource({ source: 'catalog' }, 'all'), true)
    assert.equal(matchesModelSource({ source: 'user' }, 'all'), true)
    assert.equal(matchesModelSource({}, 'all'), true)
  })

  test('the user filter keeps only imported models', () => {
    assert.equal(matchesModelSource({ source: 'user' }, 'user'), true)
    assert.equal(matchesModelSource({ source: 'catalog' }, 'user'), false)
  })

  test('the catalog filter excludes imported models', () => {
    assert.equal(matchesModelSource({ source: 'catalog' }, 'catalog'), true)
    assert.equal(matchesModelSource({ source: 'user' }, 'catalog'), false)
  })

  test('models cached before the source field existed still count as catalog models', () => {
    // The persisted model cache predates `source`, so restored entries carry none. Testing for
    // `source === 'catalog'` would make the whole library vanish under the catalog filter until
    // the next refetch.
    assert.equal(matchesModelSource({}, 'catalog'), true)
    assert.equal(matchesModelSource({ source: undefined }, 'catalog'), true)
    assert.equal(matchesModelSource({}, 'user'), false)
  })

  test('an unknown source value is treated as a catalog model', () => {
    // Forward compatibility: a source this build does not know must not disappear from the
    // default catalog view, which is where users expect to find everything.
    assert.equal(matchesModelSource({ source: 'something-new' }, 'catalog'), true)
    assert.equal(matchesModelSource({ source: 'something-new' }, 'user'), false)
  })

  test('a missing model never matches a narrowing filter', () => {
    assert.equal(matchesModelSource(null, 'user'), false)
    assert.equal(matchesModelSource(undefined, 'all'), true)
  })

  test('removes ensemble models that are no longer available', () => {
    assert.deepEqual(
      retainAvailableModelNames(['model-a', 'deleted-model', 'model-b'], ['model-b', 'model-a']),
      ['model-a', 'model-b'],
    )
  })

  test('keeps selection order when all ensemble models remain available', () => {
    assert.deepEqual(
      retainAvailableModelNames(['model-b', 'model-a'], new Set(['model-a', 'model-b'])),
      ['model-b', 'model-a'],
    )
  })
})

describe('Model sorting', () => {
  const compare = new Intl.Collator('en', { numeric: true, sensitivity: 'base' }).compare
  const full = { name: 'mvsep_mega_model_bs_roformer_53_stems_v1.ckpt', sortKey: 'bs_mega_53stem_000' }
  const child = { name: 'bs_mega_53stem_bass_mvsep.ckpt', sortKey: 'bs_mega_53stem_100_bs_mega_53stem_bass_mvsep.ckpt' }
  const ordered = (a: typeof full, b: typeof full) => compareModelDefaultOrder(a, b, compare)

  test('catalog ordering puts the full family member before individual targets', () => {
    assert.deepEqual([child, full].sort(ordered), [full, child])
    assert.ok(compare(child.name, full.name) < 0)
  })

  test('old kernels and imported models without sort metadata retain name ordering', () => {
    assert.ok(compareModelDefaultOrder({ name: 'model2' }, { name: 'model10', sortKey: '' }, compare) < 0)
    assert.equal(compareModelDefaultOrder({ name: 'same', sortKey: null }, { name: 'same' }, compare), 0)
  })

  test('the mixed-family ordering is deterministic, antisymmetric and transitive', () => {
    const rows = [full, child, { name: 'c_model', sortKey: '' }, { name: 'Model', sortKey: 'same' }, { name: 'model', sortKey: 'same' },
      ...Array.from({ length: 18 }, (_, i) => ({ name: `model${i}`, sortKey: i % 3 === 0 ? `group_${i % 4}` : '' }))]
    for (const a of rows) for (const b of rows) {
      assert.equal(Math.sign(ordered(a, b)) + Math.sign(ordered(b, a)), 0)
      for (const c of rows) if (ordered(a, b) <= 0 && ordered(b, c) <= 0) assert.ok(ordered(a, c) <= 0)
    }
    assert.deepEqual([...rows].reverse().sort(ordered), [...rows].sort(ordered))
  })
})
