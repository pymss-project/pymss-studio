import assert from 'node:assert/strict'
import test, { after, beforeEach } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createPinia } from 'pinia'
import { createServer } from 'vite'
import { createWorkflowMetadata, normalizeWorkflowMetadata, parseWorkflowFile, serializeWorkflowFile } from '../src/workflows/metadata.ts'

const vite = await createServer({
  configFile: false,
  server: { middlewareMode: true, watch: null, hmr: false, ws: false, preTransformRequests: false },
  appType: 'custom', optimizeDeps: { noDiscovery: true },
  resolve: { alias: { '@': fileURLToPath(new URL('../src', import.meta.url)) } },
})
after(() => vite.close())
const { useWorkflowStore, WorkflowGuidConflictError } = await vite.ssrLoadModule('/src/stores/workflow.ts')
const { prepareWorkflowDefinitionForRun } = await vite.ssrLoadModule('/src/workflows/runtimeDefinition.ts')
const { isSimpleWorkflowDefinition, isGraphWorkflowDefinition } = await vite.ssrLoadModule('/src/workflows/formats.ts')
const key = 'pymss-studio:workflow-state'
let storage
const originalStorage = globalThis.localStorage
beforeEach(() => {
  storage = new Map()
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) }
})
after(() => { if (originalStorage) globalThis.localStorage = originalStorage; else delete globalThis.localStorage })
const store = () => useWorkflowStore(createPinia())
const simple = () => ({ version: 1, steps: [], studio: { editor: 'simple' } })
const metadata = () => ({ ...createWorkflowMetadata(), author: '发布者', version: '1.0.0 beta', studioVersion: '0.0.25' })

test('standard JSON exports put metadata first and keep schema versions and definitions intact', () => {
  for (const definition of [simple(), { version: 0.4, nodes: [], links: [], extra: { appDefaults: { device: 'cpu' } } }]) {
    const header = metadata()
    const text = serializeWorkflowFile({ ...definition, _metadata: { guid: 'stale' } }, header)
    const json = JSON.parse(text)
    assert.equal(Object.keys(json)[0], '_metadata')
    assert.deepEqual(json._metadata, header)
    assert.deepEqual(parseWorkflowFile(text), { definition, metadata: header })
    assert.equal(json.version, definition.version)
    assert.equal(isSimpleWorkflowDefinition(json), isSimpleWorkflowDefinition(definition))
    assert.equal(isGraphWorkflowDefinition(json), isGraphWorkflowDefinition(definition))
    const defaults = { device: 'cpu', outputFormat: 'wav' }
    assert.deepEqual(prepareWorkflowDefinitionForRun(json, defaults), prepareWorkflowDefinitionForRun(definition, defaults))
    assert.equal('_metadata' in prepareWorkflowDefinitionForRun(json, defaults), false)
  }
})

test('legacy files remain readable and malformed metadata is rejected without treating it as execution data', () => {
  assert.deepEqual(parseWorkflowFile(JSON.stringify(simple())), { definition: simple(), metadata: undefined })
  for (const value of [null, [], { guid: 'invalid' }, { ...metadata(), author: 'bad\nname' }, { ...metadata(), version: 'x'.repeat(65) }]) {
    assert.equal(normalizeWorkflowMetadata(value), undefined)
    assert.throws(() => parseWorkflowFile(JSON.stringify({ ...simple(), _metadata: value })), /metadata/)
  }
  assert.throws(() => parseWorkflowFile('[]'), /definition/)
  const header = metadata()
  assert.deepEqual(normalizeWorkflowMetadata({ ...header, guid: header.guid.toUpperCase(), author: '  作者  ', version: '' }), {
    guid: header.guid, author: '作者', studioVersion: header.studioVersion,
  })
})

test('import, reload and graph edits retain the public GUID; duplicates get a new identity', async () => {
  const workflow = store()
  await workflow.initialize()
  const header = metadata()
  const imported = parseWorkflowFile(serializeWorkflowFile(simple(), header))
  const entry = await workflow.saveWorkflow({ name: 'Shared', ...imported })
  assert.deepEqual(entry.metadata, header)
  assert.equal('_metadata' in entry.definition, false)
  const reloaded = store()
  await reloaded.initialize()
  assert.deepEqual(reloaded.workflows[0].metadata, header)
  const edited = await reloaded.saveWorkflow({ ...reloaded.workflows[0], name: 'Edited', expectedUpdatedAt: entry.updatedAt })
  assert.equal(edited.metadata.guid, header.guid)
  const duplicate = await reloaded.duplicateWorkflow(entry.id)
  assert.notEqual(duplicate.metadata.guid, header.guid)
  assert.equal(duplicate.metadata.author, header.author)
  assert.equal(duplicate.metadata.version, header.version)
  assert.deepEqual(duplicate.definition, edited.definition)
})

test('publication changes preserve selection and graph revision and survive an older editor save', async () => {
  const overview = store()
  await overview.initialize()
  const first = await overview.saveWorkflow({ name: 'First', definition: simple() })
  const second = await overview.saveWorkflow({ name: 'Second', definition: simple() })
  const editor = store()
  await editor.initialize()
  const stale = JSON.parse(JSON.stringify(editor.workflows.find(item => item.id === first.id)))
  const published = await overview.updateWorkflowMetadata(first.id, { ...metadata(), author: 'Updated author' })
  assert.equal(published.metadata.guid, first.metadata.guid)
  assert.equal(published.updatedAt, first.updatedAt)
  assert.deepEqual(published.definition, first.definition)
  assert.equal(overview.selectedWorkflowId, second.id)
  const saved = await editor.saveWorkflow({ ...stale, name: 'Graph edit', expectedUpdatedAt: stale.updatedAt })
  assert.deepEqual(saved.metadata, published.metadata)
  assert.equal(saved.name, 'Graph edit')
})

test('old entries receive a stable identity on first publication, without migrating the graph', async () => {
  const legacy = { name: 'Legacy', definition: simple(), createdAt: 10, updatedAt: 20 }
  storage.set(key, JSON.stringify({ workflows: [legacy] }))
  const workflow = store()
  await workflow.initialize()
  const entry = workflow.workflows[0]
  assert.equal(entry.metadata, undefined)
  assert.equal(JSON.parse(storage.get(key)).workflows[0].metadata, undefined)
  const saved = await workflow.updateWorkflowMetadata(entry.id, metadata())
  assert.equal(saved.updatedAt, 20)
  assert.deepEqual(saved.definition, entry.definition)
  await workflow.reload()
  assert.equal(workflow.workflows[0].metadata.guid, saved.metadata.guid)
  await assert.rejects(workflow.updateWorkflowMetadata(entry.id, { guid: 'invalid' }), /metadata/)
  await workflow.deleteWorkflow(entry.id)
  await assert.rejects(workflow.updateWorkflowMetadata(entry.id, metadata()), /no longer exists/)
  assert.equal(workflow.workflows.length, 0)
})

test('cancelling or failing a file export leaves all stored publication details unchanged', async () => {
  const workflow = store()
  await workflow.initialize()
  const original = await workflow.saveWorkflow({ name: 'Shared', definition: simple(), metadata: metadata() })
  const before = storage.get(key)
  const changed = { ...original.metadata, author: 'Unpublished author', version: '2.0.0', studioVersion: '0.0.99' }
  const cancelled = await workflow.exportWorkflow(original.id, changed, async (entry, prepared) => {
    assert.deepEqual(entry.metadata, original.metadata)
    assert.deepEqual(prepared, changed)
    assert.equal(storage.get(key), before)
    return false
  })
  assert.equal(cancelled, null)
  assert.equal(storage.get(key), before)
  await assert.rejects(workflow.exportWorkflow(original.id, changed, async () => {
    throw new Error('File access denied')
  }), /File access denied/)
  assert.equal(storage.get(key), before)
  assert.deepEqual(workflow.workflows[0].metadata, original.metadata)
  assert.equal(workflow.isSaving, false)
})

test('a successful export uses the latest graph and commits the exact exported metadata afterwards', async () => {
  const overview = store()
  await overview.initialize()
  const original = await overview.saveWorkflow({ name: 'Shared', definition: simple(), metadata: metadata() })
  const editor = store()
  await editor.initialize()
  const edited = await editor.saveWorkflow({ ...editor.workflows[0], name: 'Latest name',
    definition: { ...simple(), defaults: { device: 'cpu' } }, expectedUpdatedAt: original.updatedAt })
  const before = storage.get(key)
  let exported
  const saved = await overview.exportWorkflow(original.id, { ...metadata(), author: '  Publisher  ', studioVersion: '0.0.99' }, async (entry, prepared) => {
    assert.equal(storage.get(key), before)
    assert.equal(entry.name, edited.name)
    assert.deepEqual(entry.definition, edited.definition)
    assert.equal(prepared.guid, original.metadata.guid)
    exported = JSON.parse(serializeWorkflowFile(entry.definition, prepared))
    return true
  })
  assert.deepEqual(saved.metadata, exported._metadata)
  assert.equal(saved.metadata.author, 'Publisher')
  assert.equal(saved.updatedAt, edited.updatedAt)
  assert.deepEqual(saved.definition, edited.definition)
  assert.deepEqual(JSON.parse(storage.get(key)).workflows[0].metadata, exported._metadata)
})

test('cancelled legacy exports do not assign IDs or metadata and retain later save references', async () => {
  storage.set(key, JSON.stringify({ workflows: [
    { name: 'Legacy', definition: simple(), createdAt: 10, updatedAt: 20 },
    { name: 'Other legacy', definition: simple(), createdAt: 11, updatedAt: 21 },
  ] }))
  const workflow = store()
  await workflow.initialize()
  const entry = workflow.workflows.find(item => item.name === 'Legacy')
  const other = workflow.workflows.find(item => item.name === 'Other legacy')
  const before = storage.get(key)
  assert.equal(await workflow.exportWorkflow(entry.id, metadata(), async () => false), null)
  assert.equal(storage.get(key), before)
  assert.equal(workflow.workflows.find(item => item.id === entry.id).metadata, undefined)
  await workflow.saveWorkflow({ ...other, name: 'Other edited', expectedUpdatedAt: other.updatedAt })
  assert.equal(workflow.workflows.length, 2)
  const exported = await workflow.exportWorkflow(workflow.workflows.find(item => item.name === 'Legacy').id, metadata(), async () => true)
  assert.ok(exported.metadata.guid)
  assert.equal(exported.updatedAt, 20)
})

test('an export metadata write failure preserves the previous publication record', async () => {
  const workflow = store()
  await workflow.initialize()
  const original = await workflow.saveWorkflow({ name: 'Shared', definition: simple(), metadata: metadata() })
  const before = storage.get(key)
  let exported = false
  globalThis.localStorage.setItem = () => { throw new Error('Store write denied') }
  await assert.rejects(workflow.exportWorkflow(original.id, { ...metadata(), studioVersion: '0.0.99' }, async () => {
    exported = true
    return true
  }), /Store write denied/)
  assert.equal(exported, true)
  assert.equal(storage.get(key), before)
  assert.deepEqual(workflow.workflows[0].metadata, original.metadata)
})

test('an editor assigning a GUID during first export rejects publication and a retry uses the current GUID', async () => {
  storage.set(key, JSON.stringify({ workflows: [{ id: 'old', name: 'Old workflow',
    definition: simple(), createdAt: 10, updatedAt: 20 }], selectedWorkflowId: 'old' }))
  const overview = store(), editor = store()
  await Promise.all([overview.initialize(), editor.initialize()])
  const candidate = metadata()
  let exported, edited
  await assert.rejects(overview.exportWorkflow('old', candidate, async (entry, prepared) => {
    exported = JSON.parse(serializeWorkflowFile(entry.definition, prepared))
    edited = await editor.saveWorkflow({ ...editor.workflows[0], name: 'Edited while exporting', expectedUpdatedAt: 20 })
    return true
  }), error => error instanceof WorkflowGuidConflictError && error.actualGuid === edited.metadata.guid)
  const persisted = JSON.parse(storage.get(key)).workflows[0]
  assert.notEqual(exported._metadata.guid, persisted.metadata.guid)
  assert.deepEqual(persisted.metadata, edited.metadata)
  assert.equal(persisted.metadata.author, undefined)
  assert.equal(persisted.name, 'Edited while exporting')
  assert.equal(overview.lastMutationError.code, 'WORKFLOW_METADATA_GUID_CONFLICT')

  const retried = await overview.exportWorkflow('old', candidate, async (entry, prepared) => {
    exported = JSON.parse(serializeWorkflowFile(entry.definition, prepared))
    assert.equal(prepared.guid, edited.metadata.guid)
    return true
  })
  assert.deepEqual(retried.metadata, exported._metadata)
  assert.equal(retried.metadata.guid, edited.metadata.guid)
  assert.equal(retried.updatedAt, edited.updatedAt)
})

test('an editor first-saving after export keeps the published GUID and details', async () => {
  storage.set(key, JSON.stringify({ workflows: [{ id: 'old', name: 'Old workflow',
    definition: simple(), createdAt: 10, updatedAt: 20 }], selectedWorkflowId: 'old' }))
  const overview = store(), editor = store()
  await Promise.all([overview.initialize(), editor.initialize()])
  let exported
  const published = await overview.exportWorkflow('old', metadata(), async (entry, prepared) => {
    exported = JSON.parse(serializeWorkflowFile(entry.definition, prepared))
    return true
  })
  const edited = await editor.saveWorkflow({ ...editor.workflows[0], name: 'Edited later', expectedUpdatedAt: 20 })
  assert.deepEqual(edited.metadata, exported._metadata)
  assert.deepEqual(edited.metadata, published.metadata)
  assert.equal(edited.name, 'Edited later')
})
