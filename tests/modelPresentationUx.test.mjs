import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { buildModelCategoryOptionsFromModels } from '../src/utils/modelCategory.ts'
import { compareModelDefaultOrder } from '../src/utils/modelSort.ts'

function computedValue(file, name, scope) {
  const script = readFileSync(new URL(file, import.meta.url), 'utf8').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  const source = ts.createSourceFile(file, script, ts.ScriptTarget.Latest, true)
  const statement = source.statements.find(item => ts.isVariableStatement(item) && item.declarationList.declarations.some(item => item.name.getText(source) === name))
  assert.ok(statement)
  const code = ts.transpileModule(statement.getText(source), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  return vm.runInNewContext(`${code}\n${name}`, { computed: callback => callback(), ...scope })
}

const compare = new Intl.Collator('en', { numeric: true, sensitivity: 'base' }).compare
const full = { name: 'mvsep_mega_model_bs_roformer_53_stems_v1.ckpt', sortKey: 'bs_mega_53stem_000', downloaded: true }
const child = { name: 'bs_mega_53stem_bass_mvsep.ckpt', sortKey: 'bs_mega_53stem_100_bs_mega_53stem_bass_mvsep.ckpt', downloaded: true }
function library(rows, mode = 'default', favorites = []) {
  return computedValue('../src/views/ModelsView.vue', 'sortedModels', {
    downloadedOnly: { value: false }, filteredModels: { value: rows }, modelSort: { value: mode },
    isFavoriteModel: model => favorites.includes(model.name), categoryLabel: () => '', collator: { value: { compare } },
    compareModelName: (a, b) => compare(a.name, b.name), compareModelDefaultOrder,
  }).map(row => row.name)
}

test('the library honors catalog ordering only under default sorting', () => {
  assert.deepEqual([...library([child, full])], [full.name, child.name])
  assert.deepEqual([...library([child, full], 'name-asc')], [child.name, full.name])
  assert.deepEqual([...library([child, full], 'favorite')], [child.name, full.name])
  assert.deepEqual([...library([child, full], 'default', [child.name])], [child.name, full.name])
  assert.deepEqual([...library([child, { ...full, downloaded: false }])], [child.name, full.name])
})

test('the separation picker keeps usage priorities and explicit name ordering', () => {
  function picker(mode, preferences = {}) {
    return computedValue('../src/views/SeparateView.vue', 'listedDownloadedModels', {
      downloadedModels: { value: [child, full] }, modelPreferences: { value: preferences }, modelListSortMode: { value: mode },
      compareModelName: compare, compareModelDefaultOrder,
    }).map(row => row.name)
  }
  assert.deepEqual([...picker('usage')], [full.name, child.name])
  assert.deepEqual([...picker('usage', { [child.name]: { useCount: 1 } })], [child.name, full.name])
  assert.deepEqual([...picker('name-asc')], [child.name, full.name])
})

test('both pages generate the same grouped categories from the same available models', () => {
  const rows = [{ primaryCategory: 'vocal', primaryCategoryCn: '人声', secondaryCategory: 'duet', secondaryCategoryCn: '双人分离' },
    { primaryCategory: 'instrumental', primaryCategoryCn: '伴奏', secondaryCategoryCn: '伴奏' }]
  const shared = { locale: { value: 'zh-CN' }, t: (key, args) => args ? `全部${args.category}` : '全部', buildModelCategoryOptionsFromModels }
  const library = computedValue('../src/views/ModelsView.vue', 'categoryOptions', { ...shared, modelStore: { models: rows } })
  const picker = computedValue('../src/views/SeparateView.vue', 'modelCategoryOptions', { ...shared, downloadedModels: { value: rows } })
  assert.deepEqual(library, picker)
})
