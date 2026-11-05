import assert from 'node:assert/strict'
import test from 'node:test'
import { compareModelDefaultOrder } from '../src/utils/modelSort.ts'

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
