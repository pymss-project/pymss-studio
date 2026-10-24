import assert from 'node:assert/strict'
import test from 'node:test'

// Routing rules for plugin view placements, mirroring PluginsView.openPluginView
// and the ToolsView/PluginWorkspaceView split.

function openPluginView(plugin, view) {
  const placements = view.placements || ['tool']
  if (placements.includes('workspace')) {
    return { name: 'plugin-view', params: { plugin: plugin.name, viewId: view.id } }
  }
  return { name: 'tools', query: { pluginView: `${plugin.name}/${view.id}` } }
}

test('workspace placement opens the full-page route', () => {
  const target = openPluginView({ name: 'vocal-chain' }, { id: 'editor', placements: ['tool', 'workspace'] })
  assert.deepEqual(target, { name: 'plugin-view', params: { plugin: 'vocal-chain', viewId: 'editor' } })
})

test('tool-only placement opens inside the tools page', () => {
  const target = openPluginView({ name: 'loudnorm' }, { id: 'panel' })
  assert.deepEqual(target, { name: 'tools', query: { pluginView: 'loudnorm/panel' } })
})

test('tool-only placement with explicit placements still routes to tools', () => {
  const target = openPluginView({ name: 'x' }, { id: 'v', placements: ['tool'] })
  assert.deepEqual(target, { name: 'tools', query: { pluginView: 'x/v' } })
})

// Query-param handling in ToolsView: unknown ids must not flip the active view.
function acceptsQuery(active, views, query) {
  if (typeof query === 'string' && views.some(entry => `${entry.pluginName}/${entry.view.id}` === query)) {
    return query
  }
  return active
}

test('tools page ignores pluginView query ids that are not installed', () => {
  const views = [{ pluginName: 'a', view: { id: '1' } }]
  assert.equal(acceptsQuery('', views, 'a/1'), 'a/1')
  assert.equal(acceptsQuery('', views, 'missing/9'), '')
  assert.equal(acceptsQuery('', views, undefined), '')
})
