import assert from 'node:assert/strict'
import test from 'node:test'
import { buildPluginLibrary, installedPluginKey, pluginNeedsAttention } from '../src/features/plugin-marketplace/library.ts'

const repository = 'https://github.com/pymss-project/pymss-plugins'
function installed(patch = {}) {
  return { name: 'pymss.analysis', version: '0.2.0', source: 'git', url: repository,
    sourceSubpath: 'plugins/analysis', path: '/runtime/plugins/pymss.analysis', managed: true,
    loaded: true, enabled: true, prepared: true, error: null, operations: ['measure'], ...patch }
}
function catalog(patch = {}) {
  return { name: 'pymss.analysis', url: repository, subpath: 'plugins/analysis', description: 'Catalog description',
    tags: ['analysis'], ...patch }
}

test('the offline library retains managed, external and read-only installations', () => {
  const plugins = [installed(), installed({ name: 'custom', url: '/custom/source' }),
    installed({ managed: false, path: '/home/plugins/analysis' })]
  const entries = buildPluginLibrary(plugins, [])
  assert.equal(entries.length, 3)
  assert.equal(new Set(entries.map(entry => entry.key)).size, 3)
  for (const [index, entry] of entries.entries()) {
    assert.strictEqual(entry.installed, plugins[index])
    assert.equal(entry.catalog, undefined)
    assert.deepEqual(entry.otherSources, [])
  }
})

test('matching sources combine metadata without replacing actual installed capabilities or locales', () => {
  const plugin = installed({ locales: { en: { title: 'Installed title' } }, contributions: { views: [] } })
  const listing = catalog({ locales: { en: { title: 'Market title' } } })
  const [entry] = buildPluginLibrary([plugin], [listing])
  assert.strictEqual(entry.installed, plugin)
  assert.strictEqual(entry.catalog, listing)
  assert.equal(entry.installed.version, '0.2.0')
  assert.deepEqual(entry.installed.operations, ['measure'])
  assert.equal(entry.installed.locales.en.title, 'Installed title')
  assert.deepEqual(entry.installed.contributions.views, [])
})

test('official mirrors share a listing while other same-ID sources stay inside the actual installation', () => {
  for (const url of ['https://gitcode.com/pymss-project/pymss-plugins.git/', 'https://www.github.com/PYMSS-PROJECT/PYMSS-PLUGINS']) {
    assert.equal(buildPluginLibrary([installed()], [catalog({ url })]).length, 1)
  }
  for (const url of ['https://github.com/custom/pymss-plugins', 'https://gitcode.com/custom/pymss-plugins',
    'http://gitcode.com/pymss-project/pymss-plugins', 'https://github.com/pymss-project/pymss-plugins?ref=other']) {
    const entries = buildPluginLibrary([installed()], [catalog({ url })])
    assert.equal(entries.length, 1)
    assert.equal(entries[0].catalog, undefined)
    assert.equal(entries[0].otherSources[0].url, url)
    assert.equal(entries[0].key, installedPluginKey(installed()))
  }
})

test('managed bundled defaults merge with their official listing while retaining the actual installed data', () => {
  const plugin = installed({ source: 'path', url: '/Applications/Studio.app/Contents/Resources/plugins/analysis',
    sourceSubpath: null, preinstalled: true, operations: ['installed.measure'], version: '0.1.0' })
  for (const url of [repository, 'https://gitcode.com/pymss-project/pymss-plugins.git']) {
    const listing = catalog({ url })
    const entries = buildPluginLibrary([plugin], [listing])
    assert.equal(entries.length, 1)
    assert.strictEqual(entries[0].installed, plugin)
    assert.strictEqual(entries[0].catalog, listing)
    assert.deepEqual(entries[0].otherSources, [])
    assert.equal(entries[0].installed.version, '0.1.0')
    assert.deepEqual(entries[0].installed.operations, ['installed.measure'])
    assert.equal(entries[0].key, buildPluginLibrary([plugin], [])[0].key)
  }
})

test('bundled-source matching does not extend to custom catalogs, ordinary local installs or read-only copies', () => {
  const plugin = installed({ source: 'path', url: '/bundled/plugins/analysis', sourceSubpath: null, preinstalled: true })
  for (const url of ['/custom/plugins', 'https://github.com/custom/pymss-plugins']) {
    const entries = buildPluginLibrary([plugin], [catalog({ url })])
    assert.equal(entries.length, 1)
    assert.equal(entries[0].catalog, undefined)
    assert.equal(entries[0].otherSources[0].url, url)
  }
  assert.equal(buildPluginLibrary([{ ...plugin, preinstalled: false }], [catalog()])[0].catalog, undefined)
  const readonly = buildPluginLibrary([{ ...plugin, managed: false }], [catalog()])
  assert.equal(readonly.length, 2)
  assert.equal(readonly[0].catalog, undefined)
  assert.deepEqual(readonly[1].otherSources, [])
})

test('matching plugin IDs require the same selected subdirectory', () => {
  const entries = buildPluginLibrary([installed()], [catalog({ subpath: 'plugins/other' })])
  assert.equal(entries.length, 1)
  assert.equal(entries[0].catalog, undefined)
  assert.equal(entries[0].otherSources[0].subpath, 'plugins/other')
  assert.equal(buildPluginLibrary([installed()], [catalog({ subpath: './plugins/analysis/' })]).length, 1)
})

test('unprepared intent entries with source metadata still merge and keep the same key', () => {
  const plugin = installed({ prepared: false, loaded: false, path: '' })
  const available = buildPluginLibrary([], [catalog()])[0]
  const prepared = buildPluginLibrary([plugin], [catalog()])[0]
  const offline = buildPluginLibrary([plugin], [])[0]
  assert.equal(prepared.key, available.key)
  assert.equal(prepared.key, offline.key)
  assert.equal(prepared.catalog.name, plugin.name)
  assert.equal(installedPluginKey(plugin), installedPluginKey({ ...plugin, enabled: false, path: '/new/runtime' }))
})

test('read-only same-ID copies never consume catalog rows and include normalized paths in their keys', () => {
  const plugins = [installed({ managed: false, path: '/plugins/a' }), installed({ managed: false, path: '/plugins/b' })]
  const entries = buildPluginLibrary(plugins, [catalog()])
  assert.equal(entries.length, 3)
  assert.equal(new Set(entries.map(entry => entry.key)).size, 3)
  assert.equal(entries[0].catalog, undefined)
  assert.equal(entries[1].catalog, undefined)
  assert.deepEqual(entries[2].otherSources, [])
  assert.equal(entries[2].key, installedPluginKey(installed()))
})

test('Windows drive and verbatim UNC source paths normalize independently of the host OS', () => {
  for (const [local, listing] of [
    ['C:\\Plugin Sources\\Repo', 'c:/plugin sources/repo/'],
    ['\\\\?\\D:\\Sources\\Repo', 'd:/sources/repo/'],
    ['\\\\?\\UNC\\SERVER\\Share\\Repo', '\\\\server\\share\\repo\\'],
    ['\\\\Server\\Share\\..\\Repo', '\\\\server\\share\\repo'],
  ]) {
    const plugin = installed({ url: local, sourceSubpath: 'Plugins/Analysis' })
    assert.equal(buildPluginLibrary([plugin], [catalog({ url: listing, subpath: 'plugins/analysis' })]).length, 1)
    const plain = { ...plugin, managed: false, path: listing }
    assert.equal(installedPluginKey({ ...plain, path: local }), installedPluginKey(plain))
    assert.ok(!installedPluginKey(plain).includes(' '))
  }
})

test('POSIX acquisition and installation paths preserve case', () => {
  const plugin = installed({ url: '/Sources/Repo' })
  assert.equal(buildPluginLibrary([plugin], [catalog({ url: '/sources/repo' })])[0].catalog, undefined)
  assert.equal(buildPluginLibrary([plugin], [catalog({ url: '/Sources/Repo', subpath: 'Plugins/Analysis' })])[0].catalog, undefined)
  assert.notEqual(installedPluginKey({ ...plugin, managed: false, path: '/Plugins/a' }),
    installedPluginKey({ ...plugin, managed: false, path: '/plugins/a' }))
})

test('POSIX backslashes remain literal characters in source paths and read-only keys', () => {
  const plugin = installed({ url: '/tmp/Foo\\bar' })
  assert.equal(buildPluginLibrary([plugin], [catalog({ url: '/tmp/Foo/bar' })])[0].catalog, undefined)
  assert.equal(buildPluginLibrary([plugin], [catalog({ url: '/tmp/Foo\\bar' })]).length, 1)
  assert.notEqual(installedPluginKey({ ...plugin, managed: false, path: '/tmp/Foo\\bar' }),
    installedPluginKey({ ...plugin, managed: false, path: '/tmp/Foo/bar' }))
  assert.equal(buildPluginLibrary([installed({ url: 'C:\\sources', sourceSubpath: 'plugins\\analysis' })],
    [catalog({ url: 'c:/sources', subpath: 'plugins/analysis' })]).length, 1)
})

test('attention identifies actionable installation failures without classifying disabled or CLI-only plugins as broken', () => {
  for (const patch of [{ prepared: false }, { loaded: false }, { availability: 'unavailable' }, { compatibility: { compatible: false, reasons: ['backend'] } },
    { error: 'Load failed' }, { dependencyError: 'Missing dependency' }, { viewError: 'Unsupported view' }, { removalPending: true }]) {
    assert.equal(pluginNeedsAttention(installed(patch)), true)
    if (!patch.removalPending) assert.equal(pluginNeedsAttention(installed({ ...patch, enabled: false })), false)
  }
  assert.equal(pluginNeedsAttention(installed({ contributions: { views: [] }, operations: ['cli.operation'] })), false)
  assert.equal(pluginNeedsAttention(installed({ loaded: null })), false)
  assert.equal(pluginNeedsAttention(installed({ enabled: false, removalPending: true })), true)
})

test('reviewed updates and failed checks put a healthy managed plugin into attention', () => {
  for (const status of ['update', 'content']) {
    assert.equal(pluginNeedsAttention(installed(), { status, target: { schemaVersion: 1 } }), true)
    assert.equal(pluginNeedsAttention(installed(), { status }), false)
  }
  for (const status of ['latest', 'source_changed', 'older', 'incomparable']) {
    assert.equal(pluginNeedsAttention(installed(), { status, target: { schemaVersion: 1 } }), false)
  }
  assert.equal(pluginNeedsAttention(installed(), { status: 'error', error: 'Offline' }), true)
})

test('a managed update never marks a healthy read-only copy with the same ID as needing attention', () => {
  const managed = installed()
  const readonly = installed({ managed: false, path: '/external/plugins/pymss.analysis' })
  for (const status of ['update', 'content', 'error']) {
    const update = { status, target: { schemaVersion: 1 } }
    assert.equal(pluginNeedsAttention(managed, update), true)
    assert.equal(pluginNeedsAttention(readonly, update), false)
  }
  assert.equal(pluginNeedsAttention({ ...readonly, error: 'Load failed' }), true)
})
