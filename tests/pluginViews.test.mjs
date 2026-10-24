import assert from 'node:assert/strict'
import test from 'node:test'

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
