import assert from 'node:assert/strict'
import test from 'node:test'

import { useOptionalRuntimePackage } from '../src/features/runtime-packages/optionalRuntimePackage.ts'

test('optional package activity survives component consumers being replaced', () => {
  const firstPage = useOptionalRuntimePackage('funasr')
  const reopenedPage = useOptionalRuntimePackage('  FunASR  ')

  assert.strictEqual(reopenedPage, firstPage)

  firstPage.busy.value = true
  firstPage.action.value = 'install'
  firstPage.appendLog('installing', 'stage')

  assert.equal(reopenedPage.busy.value, true)
  assert.equal(reopenedPage.action.value, 'install')
  assert.equal(reopenedPage.logs.value.at(-1)?.message, 'installing')

  firstPage.busy.value = false
  firstPage.action.value = null
  firstPage.logs.value = []
})
