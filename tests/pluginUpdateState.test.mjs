import assert from 'node:assert/strict'
import test from 'node:test'
import { canApplyPluginUpdate, canCheckPluginUpdate, pluginUpdateKey, pluginUpdateState } from '../src/features/plugin-marketplace/updates.ts'

test('update checks distinguish unverified changes, failures and reviewed updates', () => {
  assert.equal(pluginUpdateState(), 'unchecked')
  assert.equal(pluginUpdateState({ up_to_date: true }), 'latest')
  assert.equal(pluginUpdateState({ up_to_date: false, remote_version: '0.1' }), 'source_changed')
  assert.equal(pluginUpdateState({ status: 'latest', error: 'Offline' }), 'error')
  for (const status of ['older', 'incomparable', 'source_changed', 'latest', 'error']) {
    assert.equal(canApplyPluginUpdate({ status, target: {} }), false)
  }
  for (const status of ['update', 'content']) {
    assert.equal(canApplyPluginUpdate({ status }), false)
    assert.equal(canApplyPluginUpdate({ status, target: { schemaVersion: 1 } }), true)
  }
})

test('update eligibility excludes read-only, missing and removed bindings', () => {
  const plugin = { managed: true, url: '/source' }
  assert.equal(canCheckPluginUpdate(plugin), true)
  for (const patch of [{ managed: false }, { prepared: false }, { removalPending: true }, { url: null }]) {
    assert.equal(canCheckPluginUpdate({ ...plugin, ...patch }), false)
  }
  assert.equal(canCheckPluginUpdate({ managed: true, preinstalled: true }), true)
})

test('cached checks bind source, installation contents and execution environment', () => {
  const plugin = { name: 'hello', managed: true, version: '1.0', url: '/source', contentDigest: 'first' }
  const runtime = { identity: 'cpu', pythonPath: '/python', updateSourceIdentity: 'source-one' }
  const key = pluginUpdateKey(plugin, runtime)
  for (const patch of [{ version: '2.0' }, { contentDigest: 'second' }, { url: '/other' }, { sourceRef: 'other' }, { installedAt: 'later' }]) {
    assert.notEqual(pluginUpdateKey({ ...plugin, ...patch }, runtime), key)
  }
  for (const patch of [{ pythonPath: '/other/python' }, { identity: 'cuda' }, { updateSourceIdentity: 'source-two' }]) {
    assert.notEqual(pluginUpdateKey(plugin, { ...runtime, ...patch }), key)
  }
  assert.equal(pluginUpdateKey({ ...plugin, loaded: false, enabled: false }, runtime), key)
})
