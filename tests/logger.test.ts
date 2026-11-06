import assert from 'node:assert/strict'
import test from 'node:test'
import { createLogger, recordFeatureVisit } from '../src/utils/logger.ts'

test('frontend logs send one sanitized structured record and retain readable console output', async t => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  const calls: { command: string; args: any }[] = []
  Object.defineProperty(globalThis, 'window', { configurable: true, value: {
    __TAURI_INTERNALS__: { invoke: async (command: string, args: unknown) => { calls.push({ command, args }) } },
  } })
  t.after(() => { if (previous) Object.defineProperty(globalThis, 'window', previous); else Reflect.deleteProperty(globalThis, 'window') })
  const output = t.mock.method(console, 'warn', () => {})
  createLogger('ui.settings').warn('Request token=private-token failed', new Error('https://name:password@example.com/resource'), { privatePayload: 'must not be serialized' })
  await Promise.resolve()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].command, 'write_frontend_log')
  const record = calls[0].args.record
  assert.equal(record.source, 'studio')
  assert.equal(record.level, 'warn')
  assert.equal(record.logger, 'ui.settings')
  assert.ok(record.id)
  assert.ok(Number.isFinite(Date.parse(record.timestamp)))
  assert.match(record.message, /token=<redacted>/)
  assert.ok(!JSON.stringify(record).includes('private-token'))
  assert.ok(!JSON.stringify(record).includes('name:password'))
  assert.ok(!JSON.stringify(record).includes('privatePayload'))
  assert.ok(!JSON.stringify(output.mock.calls).includes('private-token'))
  assert.match(String(output.mock.calls[0].arguments[0]), /Studio\/ui.settings/)
})

test('a rejected logging IPC never becomes another application failure', async t => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'window', { configurable: true, value: {
    __TAURI_INTERNALS__: { invoke: async () => { throw new Error('Log storage unavailable') } },
  } })
  t.after(() => { if (previous) Object.defineProperty(globalThis, 'window', previous); else Reflect.deleteProperty(globalThis, 'window') })
  t.mock.method(console, 'error', () => {})
  assert.doesNotThrow(() => createLogger('ui.app').error('Original operation failed'))
  await Promise.resolve()
  await Promise.resolve()
})

test('frontend credentials are fully hidden in console output, IPC messages and exception details', async t => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  const records: any[] = []
  Object.defineProperty(globalThis, 'window', { configurable: true, value: {
    __TAURI_INTERNALS__: { invoke: async (_command: string, args: any) => { records.push(args.record) } },
  } })
  t.after(() => { if (previous) Object.defineProperty(globalThis, 'window', previous); else Reflect.deleteProperty(globalThis, 'window') })
  const output = t.mock.method(console, 'warn', () => {})
  const logger = createLogger('ui.credentials')
  for (const [message, expected] of [
    ["password='prefix private-suffix' status=failed", "password='<redacted>' status=failed"],
    [String.raw`token="prefix \"quoted\" private-suffix" status=failed`, 'token="<redacted>" status=failed'],
    [String.raw`password='prefix \'quoted\' private-suffix' status=failed`, "password='<redacted>' status=failed"],
    [String.raw`password="private-suffix\\" status=failed`, 'password="<redacted>" status=failed'],
    [JSON.stringify({ password: 'prefix "quoted" private-suffix', status: 'failed' }), JSON.stringify({ password: '<redacted>', status: 'failed' })],
    ['Authorization: Digest username=example, response=private-response\nstatus=failed', 'Authorization: <redacted>\nstatus=failed'],
    ['Authorization: Basic private-basic\nstatus=failed', 'Authorization: <redacted>\nstatus=failed'],
    ...['access_token', 'refresh_token', 'credential', 'api-key', 'proxy-pass'].map(key => [`${key}="private-suffix" status=failed`, `${key}="<redacted>" status=failed`]),
  ]) {
    logger.warn(message, new Error(message))
    await Promise.resolve()
    const record = records.at(-1)
    assert.equal(record.message, expected)
    assert.ok(record.detail.includes(expected))
  }
  const allOutput = JSON.stringify({ records, console: output.mock.calls })
  for (const secret of ['private-suffix', 'private-response', 'private-basic']) assert.ok(!allOutput.includes(secret), secret)
})

test('serialized credential strings retain their wrappers and public fields through multiple encodings', async t => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  const records: any[] = []
  Object.defineProperty(globalThis, 'window', { configurable: true, value: {
    __TAURI_INTERNALS__: { invoke: async (_command: string, args: any) => { records.push(args.record) } },
  } })
  t.after(() => { if (previous) Object.defineProperty(globalThis, 'window', previous); else Reflect.deleteProperty(globalThis, 'window') })
  const output = t.mock.method(console, 'error', () => {})
  const singleQuote = (value: string) => `'${value.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`
  const secret = JSON.stringify({ password: 'prefix "quoted" private-suffix', token: "prefix 'quoted' private-token", status: 'failed' })
  const safe = JSON.stringify({ password: '<redacted>', token: '<redacted>', status: 'failed' })
  for (const encode of [JSON.stringify, singleQuote]) {
    let encoded = secret
    let expected = safe
    for (let depth = 0; depth < 4; depth += 1) {
      encoded = encode(encoded)
      expected = encode(expected)
      createLogger('ui.credentials').error(`Request rejected: ${encoded}`)
      await Promise.resolve()
      assert.equal(records.at(-1).message, `Request rejected: ${expected}`)
    }
  }
  const allOutput = JSON.stringify({ records, console: output.mock.calls })
  assert.ok(!allOutput.includes('private-suffix'))
  assert.ok(!allOutput.includes('private-token'))
  createLogger('ui.credentials').error(`payload${JSON.stringify(secret)}`)
  await Promise.resolve()
  assert.equal(records.at(-1).message, `payload${JSON.stringify(safe)}`)
})

test('serialized credentials remain hidden at the nesting limit without renaming bare keys', async t => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  const records: any[] = []
  Object.defineProperty(globalThis, 'window', { configurable: true, value: {
    __TAURI_INTERNALS__: { invoke: async (_command: string, args: any) => { records.push(args.record) } },
  } })
  t.after(() => { if (previous) Object.defineProperty(globalThis, 'window', previous); else Reflect.deleteProperty(globalThis, 'window') })
  const output = t.mock.method(console, 'warn', () => {})
  const singleQuote = (value: string) => `'${value.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`
  for (const encoders of [[JSON.stringify], [singleQuote], [JSON.stringify, singleQuote]]) {
    let message = JSON.stringify({ password: 'prefix "quoted" private-suffix', status: 'failed' })
    let expected = JSON.stringify({ password: '<redacted>', status: 'failed' })
    for (let depth = 0; depth <= 10; depth += 1) {
      createLogger('ui.credentials').warn(`It's unavailable: ${message}`)
      await Promise.resolve()
      const record = records.at(-1)
      assert.ok(!record.message.includes('private-suffix'), `depth=${depth}`)
      if (depth <= 8) assert.equal(record.message, `It's unavailable: ${expected}`)
      const encode = encoders[depth % encoders.length]!
      message = encode(message)
      expected = encode(expected)
    }
  }
  assert.ok(!JSON.stringify({ records, console: output.mock.calls }).includes('private-suffix'))
})

test('feature visits record validated plugin view identifiers', async t => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  const calls: { command: string; args: any }[] = []
  Object.defineProperty(globalThis, 'window', { configurable: true, value: {
    __TAURI_INTERNALS__: { invoke: async (command: string, args: unknown) => { calls.push({ command, args }) } },
  } })
  t.after(() => { if (previous) Object.defineProperty(globalThis, 'window', previous); else Reflect.deleteProperty(globalThis, 'window') })
  recordFeatureVisit('workflow-node-editor')
  recordFeatureVisit('tools', 'pymss.asr/tool')
  await Promise.resolve()
  assert.equal(calls.length, 2)
  assert.ok(calls.every(call => call.command === 'write_frontend_log'))
  const [page, tool] = calls.map(call => call.args.record)
  for (const record of [page, tool]) {
    assert.equal(record.schemaVersion, 1)
    assert.equal(record.source, 'studio')
    assert.equal(record.level, 'info')
    assert.equal(record.logger, 'ui.navigation')
    assert.equal(record.event, 'ui.navigation')
    assert.ok(record.id)
    assert.ok(Number.isFinite(Date.parse(record.timestamp)))
    assert.equal(record.detail, undefined)
  }
  assert.deepEqual(page.context, { page: 'workflow-node-editor' })
  assert.deepEqual(tool.context, { page: 'tools', tool: 'pymss.asr/tool' })
  for (const id of ['pymss.analysis/spectrum', 'author.custom-audio/panel_v2', 'custom/tool']) recordFeatureVisit('tools', id)
  await Promise.resolve()
  assert.deepEqual(calls.slice(2).map(call => call.args.record.context.tool), ['pymss.analysis/spectrum', 'author.custom-audio/panel_v2', 'custom/tool'])
})

test('unknown feature identifiers and sensitive navigation values are rejected', async t => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  const calls: unknown[] = []
  Object.defineProperty(globalThis, 'window', { configurable: true, value: {
    __TAURI_INTERNALS__: { invoke: async (...args: unknown[]) => { calls.push(args) } },
  } })
  t.after(() => { if (previous) Object.defineProperty(globalThis, 'window', previous); else Reflect.deleteProperty(globalThis, 'window') })
  for (const page of ['', 'unknown', '/editor?projectId=private-project', 'https://example.com/?token=private', 'D:\\private\\audio.wav']) recordFeatureVisit(page)
  for (const tool of ['', 'unknown', 'asr?token=private', 'D:\\private\\model.pt']) recordFeatureVisit('tools', tool)
  for (const tool of ['asr', 'pymss.asr/tool?token=private', 'pymss.asr/../private', 'pymss.asr/.private',
    'pymss.asr/tool\n', 'pymss.asr/tool\r', 'pymss.asr/tool/private', `${'x'.repeat(256)}/tool`]) recordFeatureVisit('tools', tool)
  recordFeatureVisit('editor', 'pymss.asr/tool')
  await Promise.resolve()
  assert.deepEqual(calls, [])
})

test('feature visits do not invoke IPC in browser previews or without a window', t => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  t.after(() => { if (previous) Object.defineProperty(globalThis, 'window', previous); else Reflect.deleteProperty(globalThis, 'window') })
  Reflect.deleteProperty(globalThis, 'window')
  assert.doesNotThrow(() => recordFeatureVisit('separate'))
  Object.defineProperty(globalThis, 'window', { configurable: true, value: {} })
  assert.doesNotThrow(() => recordFeatureVisit('tools', 'pymss.convert/tool'))
})

test('feature visit logging failures never interrupt navigation', async t => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window')
  t.after(() => { if (previous) Object.defineProperty(globalThis, 'window', previous); else Reflect.deleteProperty(globalThis, 'window') })
  for (const invoke of [
    () => { throw new Error('IPC bridge unavailable') },
    async () => { throw new Error('Log storage unavailable') },
  ]) {
    Object.defineProperty(globalThis, 'window', { configurable: true, value: { __TAURI_INTERNALS__: { invoke } } })
    assert.doesNotThrow(() => recordFeatureVisit('tools', 'pymss.convert/tool'))
    await Promise.resolve()
    await Promise.resolve()
  }
})
