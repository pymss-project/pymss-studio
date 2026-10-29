import assert from 'node:assert/strict'
import test from 'node:test'
import { safeModelName } from '../src/utils/modelName.ts'

test('custom model names cannot escape the managed directory on any supported platform', () => {
  for (const name of ['.', '..', '../sibling', '/outside', 'C:\\outside', 'a/b', 'a\\b', 'a:b', 'a*', 'a\u0000b', 'a\u001fb', 'a.', 'CON', 'con.ckpt', 'COM1', 'LPT9.yaml']) {
    assert.equal(safeModelName(name), false, name)
  }
  for (const name of ['my_model', 'model-v2.1', '人声模型', 'COM10', 'LPT10']) assert.equal(safeModelName(name), true, name)
})
