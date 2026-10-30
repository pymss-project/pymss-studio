import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { runtimeErrorDetails, runtimeErrorSummary, runtimeOperationError } from '../src/utils/runtimeErrors.ts'

const locales = Object.fromEntries(['zh-CN', 'en'].map(locale => [locale,
  JSON.parse(readFileSync(new URL(`../src/i18n/${locale}.json`, import.meta.url), 'utf8')).settings,
]))
const translate = (locale: string) => (key: string) => locales[locale][key.replace('settings.', '')]

for (const locale of ['zh-CN', 'en']) {
  test(`${locale} explains an incompatible manifest without repeating the worker's reinstall advice`, () => {
    const failure = runtimeOperationError({
      code: 'RUNTIME_MANIFEST_INCOMPATIBLE',
      message: 'The runtime manifest is newer than this application or has an unknown version. Reinstall the runtime.',
    })
    const summary = runtimeErrorSummary(failure, translate(locale))
    assert.match(summary, locale === 'zh-CN' ? /先更新 Studio/ : /Update Studio first/)
    assert.doesNotMatch(summary, /reinstall|重装/i)
    assert.equal(runtimeErrorDetails(failure), `${failure.code}\n${failure.message}`)
  })

  test(`${locale} translates runtime state and cancellation codes`, () => {
    for (const code of ['RUNTIME_UPDATE_PLAN_INVALID', 'RUNTIME_UPDATE_PLAN_STALE', 'RUNTIME_CORE_UPDATE_INACTIVE', 'RUNTIME_NOT_INSTALLED', 'RUNTIME_BUSY', 'RUNTIME_PERMISSION_DENIED', 'TASK_CANCELLED']) {
      const summary = runtimeErrorSummary({ code, message: 'raw worker error' }, translate(locale))
      assert.equal(typeof summary, 'string', code)
      assert.notEqual(summary, 'raw worker error')
      assert.notEqual(summary, locales[locale].runtimeCoreOperationFailed)
    }
  })
}

test('structured worker diagnostics and unknown errors remain inspectable', () => {
  const failure = runtimeOperationError({
    code: 'RUNTIME_MANIFEST_INCOMPATIBLE', message: 'Manifest version unsupported',
    detail: 'Original traceback', details: { status: 'unknown' },
    extra: { runtimeManifestVersion: 'future' }, appManifestVersion: '2026-10',
  })
  const detail = runtimeErrorDetails(failure)
  for (const expected of ['RUNTIME_MANIFEST_INCOMPATIBLE', 'Original traceback', 'unknown', 'future', '2026-10']) assert.ok(detail.includes(expected))
  assert.equal(runtimeOperationError(failure), failure)
  for (const unknown of [new Error('network unreachable'), 'IPC disconnected', { code: 'FUTURE_RUNTIME_ERROR', message: 'New failure', detail: 'Extra context' }, { code: '__proto__', message: 'Unknown code' }]) {
    assert.equal(runtimeErrorSummary(unknown, translate('zh-CN')), locales['zh-CN'].runtimeCoreOperationFailed)
    assert.ok(runtimeErrorDetails(unknown).includes(typeof unknown === 'string' ? unknown : unknown.message))
  }
})
