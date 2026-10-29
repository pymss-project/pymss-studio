import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// Studio plugin view SDK host bridge logic, mirroring PluginViewHost.vue:
// origin checks, permission allowlists, JSON-RPC dispatch, progress fanout.

const listeners = []
const pending = new Map()
const progressHandlers = new Map()
let nextId = 1

function permission(method, allowed) {
  const family = method.split('.')[0]
  return allowed.includes(method) || allowed.includes(family)
}

const rpc = {
  onMessage(event, frameWindow) {
    if (event.source !== frameWindow) return
    const data = event.data
    if (!data || typeof data !== 'object' || data.jsonrpc !== '2.0') return
    if (data.method === 'task.progress') {
      progressHandlers.get(String(data.params?.taskId))?.(data.params)
      return
    }
    const entry = pending.get(Number(data.id))
    if (!entry) return
    pending.delete(Number(data.id))
    if (data.error) entry.reject(new Error(String(data.error.message)))
    else entry.resolve(data.result)
  },
  request(frame, method, params, permissions) {
    if (!permission(method, permissions)) {
      return Promise.reject(new Error('PERMISSION_DENIED: ' + method))
    }
    const id = nextId++
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject })
      frame?.postMessage({ jsonrpc: '2.0', id, method, params })
    })
  },
  emitProgress(taskId, payload) {
    progressHandlers.get(String(taskId))?.(payload)
    listeners.forEach(listener => listener({ method: 'task.progress', params: { taskId, ...payload } }))
  },
  trackProgress(taskId, handler) {
    progressHandlers.set(taskId, handler)
    return () => progressHandlers.delete(taskId)
  },
}

test('permission allowlist gates methods by exact name or family', () => {
  const allowed = ['operations', 'files.pickAudio']
  assert.equal(permission('operations.start', allowed), true)
  assert.equal(permission('files.pickAudio', allowed), true)
  assert.equal(permission('files.writeFile', allowed), false)
  assert.equal(permission('app.quit', allowed), false)
})

test('origin check drops messages from foreign frames', () => {
  const frameWindow = { id: 'frame' }
  let delivered = 0
  const listener = event => {
    if (event.source !== frameWindow) return
    delivered += 1
  }
  listener({ source: 'attacker', data: { jsonrpc: '2.0', id: 1 } })
  listener({ source: frameWindow, data: { jsonrpc: '2.0', id: 1 } })
  assert.equal(delivered, 1)
})

test('requests reject with PERMISSION_DENIED before posting when not allowed', async () => {
  const posted = []
  const frame = { postMessage: m => posted.push(m) }
  await assert.rejects(
    rpc.request(frame, 'app.quit', {}, ['operations']),
    /PERMISSION_DENIED: app.quit/,
  )
  assert.deepEqual(posted, [])
})

test('task progress events fan out only to matching task handlers', () => {
  const seen = []
  rpc.trackProgress('task-a', () => seen.push('a'))
  rpc.trackProgress('task-b', () => seen.push('b'))
  rpc.emitProgress('task-a', { fraction: 0.5 })
  rpc.emitProgress('task-b', { fraction: 1 })
  rpc.emitProgress('task-c', { fraction: 0 })
  assert.deepEqual(seen, ['a', 'b'])
})

test('plugin views are only offered for managed and loaded plugins', () => {
  const rows = [
    { name: 'ok', managed: true, loaded: true, contributions: { views: [{ id: 'v', placements: ['tool'] }] } },
    { name: 'unloaded', managed: true, loaded: false, contributions: { views: [{ id: 'v2', placements: ['tool'] }] } },
    { name: 'manual', managed: false, loaded: true, contributions: { views: [{ id: 'v3', placements: ['tool'] }] } },
  ]
  const toolViews = rows
    .filter(row => row.managed && row.loaded)
    .flatMap(row => row.contributions.views
      .filter(v => v.placements.includes('tool'))
      .map(v => ({ plugin: row.name, viewId: v.id })))
  assert.deepEqual(toolViews, [{ plugin: 'ok', viewId: 'v' }])
})

function sdkHarness() {
  const sent = []
  const listeners = []
  const parent = { postMessage: message => sent.push(message) }
  const root = { style: { setProperty() {} }, dataset: {} }
  const window = { parent, addEventListener: (_name, listener) => listeners.push(listener) }
  const document = { documentElement: root, dispatchEvent() {}, addEventListener() {} }
  vm.runInNewContext(readFileSync(new URL('../src-tauri/resources/plugin-sdk/pymss-sdk.js', import.meta.url), 'utf8'), {
    window, document, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail } },
  })
  const deliver = (data, source = parent) => listeners.forEach(listener => listener({ data, source }))
  const respond = (id, result) => deliver({ jsonrpc: '2.0', id, result })
  return { sdk: window.pymss, sent, deliver, respond, parent }
}

test('published SDK exposes the extended methods with operation ids and exact payloads', async () => {
  const { sdk, sent, respond } = sdkHarness()
  const start = sdk.startOperation('pymss.convert.batch', { format: 'wav' }, { audio: 'a.wav' }, undefined, { outputDir: '/out' })
  assert.equal(typeof start.id, 'number')
  assert.equal(sent.at(-1).method, 'operations.start')
  assert.equal(JSON.stringify(sent.at(-1).params), JSON.stringify({
    operation: 'pymss.convert.batch', params: { format: 'wav' }, inputs: { audio: 'a.wav' }, outputDir: '/out',
  }))
  const cancelling = sdk.cancelOperation(start.id)
  assert.equal(JSON.stringify(sent.at(-1)), JSON.stringify({ jsonrpc: '2.0', id: cancelling.id, method: 'operations.cancel', params: { id: start.id } }))
  respond(cancelling.id, true)
  respond(start.id, { outputs: {} })
  assert.equal(await cancelling, true)
  assert.equal(JSON.stringify(await start), JSON.stringify({ outputs: {} }))

  const read = sdk.readSettings()
  assert.equal(JSON.stringify(sent.at(-1).params), JSON.stringify({}))
  respond(read.id, { format: 'wav' })
  assert.equal(JSON.stringify(await read), JSON.stringify({ format: 'wav' }))
  const write = sdk.writeSettings({ format: 'flac' })
  assert.equal(JSON.stringify(sent.at(-1).params), JSON.stringify({ data: { format: 'flac' } }))
  respond(write.id, { format: 'flac' })
  assert.equal(JSON.stringify(await write), JSON.stringify({ format: 'flac' }))

  const pick = sdk.pickAudio({ multiple: true })
  assert.equal(JSON.stringify(sent.at(-1).params), JSON.stringify({ multiple: true }))
  respond(pick.id, { paths: ['/a.wav'] })
  assert.equal(JSON.stringify(await pick), JSON.stringify({ paths: ['/a.wav'] }))
  const scan = sdk.scanAudio(['/music'], false)
  assert.equal(JSON.stringify(sent.at(-1).params), JSON.stringify({ paths: ['/music'], recursive: false }))
  respond(scan.id, { files: ['/music/a.wav'] })
  assert.equal(JSON.stringify(await scan), JSON.stringify({ files: ['/music/a.wav'] }))

  const analysis = sdk.analyzeAudio({ operation: 'tempo_key', inputs: ['/a.wav'] })
  assert.equal(JSON.stringify(sent.at(-1).params), JSON.stringify({ payload: { operation: 'tempo_key', inputs: ['/a.wav'] } }))
  respond(analysis.id, { items: [] })
  assert.equal(JSON.stringify(await analysis), JSON.stringify({ items: [] }))
  const packageStatus = sdk.packageStatus()
  assert.equal(JSON.stringify(sent.at(-1).params), JSON.stringify({ package: 'funasr' }))
  respond(packageStatus.id, { package: 'funasr', installed: true })
  assert.equal(JSON.stringify(await packageStatus), JSON.stringify({ package: 'funasr', installed: true }))
  const manage = sdk.managePackage({ package: 'funasr', action: 'install' })
  assert.equal(JSON.stringify(sent.at(-1).params), JSON.stringify({ payload: { package: 'funasr', action: 'install' } }))
  respond(manage.id, { package: 'funasr', installed: true })
  assert.equal(JSON.stringify(await manage), JSON.stringify({ package: 'funasr', installed: true }))
})

test('published SDK ignores foreign-frame responses and preserves task cancellation codes', async () => {
  const { sdk, sent, deliver, parent } = sdkHarness()
  const request = sdk.startOperation('plugin.convert', {}, {})
  const id = request.id
  deliver({ jsonrpc: '2.0', id, result: { forged: true } }, { postMessage() {} })
  await new Promise(setImmediate)
  assert.equal(sent.filter(message => message.id === id).length, 1)
  deliver({
    jsonrpc: '2.0', id,
    error: { code: -32000, message: 'Stopped', data: { code: 'TASK_CANCELLED' } },
  }, parent)
  await assert.rejects(request, error => error.message === 'Stopped' && error.code === 'TASK_CANCELLED')
})
