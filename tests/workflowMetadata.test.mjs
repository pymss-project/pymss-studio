import assert from 'node:assert/strict'
import test, { after, beforeEach } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createPinia } from 'pinia'
import { createServer } from 'vite'
import { createWorkflowMetadata, legacyWorkflowDescription, normalizeWorkflowMetadata, parseWorkflowFile, resolveWorkflowDescription, serializeWorkflowFile, WORKFLOW_NOTES_MAX_LENGTH } from '../src/workflows/metadata.ts'

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
  for (const value of [null, [], { guid: 'invalid' }, { ...metadata(), author: 'bad\nname' }, { ...metadata(), version: 'x'.repeat(65) }, { ...metadata(), legacyDescription: 42 }]) {
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

test('multiline author notes round-trip through workflow files with matching text validation', () => {
  const header = { ...metadata(), notes: '  输入要求\n\t保留原始音轨\r\n输出说明  ' }
  const normalized = { ...header, notes: header.notes.trim() }
  assert.deepEqual(parseWorkflowFile(serializeWorkflowFile(simple(), header)).metadata, normalized)
  assert.equal(normalizeWorkflowMetadata({ ...header, notes: ' \n\t ' }).notes, '')
  for (const notes of [42, [], 'bad\u0000text', 'bad\u000btext', 'bad\u007ftext']) {
    assert.equal(normalizeWorkflowMetadata({ ...header, notes }), undefined)
    assert.throws(() => parseWorkflowFile(JSON.stringify({ ...simple(), _metadata: { ...header, notes } })), /metadata/)
  }
  assert.equal(normalizeWorkflowMetadata({ ...header, notes: 'x'.repeat(WORKFLOW_NOTES_MAX_LENGTH) }).notes.length, WORKFLOW_NOTES_MAX_LENGTH)
})

test('notes edits preserve newer publication fields, graph revisions and selection across windows', async () => {
  const overview = store()
  await overview.initialize()
  const original = await overview.saveWorkflow({ name: 'Workflow', definition: simple(), metadata: metadata() })
  const other = await overview.saveWorkflow({ name: 'Other', definition: simple() })
  const notesEditor = store()
  await notesEditor.initialize()
  const stale = JSON.parse(JSON.stringify(notesEditor.workflows.find(item => item.id === original.id)))
  const published = await overview.updateWorkflowMetadata(original.id, { ...original.metadata, author: 'Current author', version: '2.0.0' })
  const saved = await notesEditor.updateWorkflowNotes(original.id, 'Input requirements\nOutput instructions')
  assert.deepEqual(saved.metadata, { ...published.metadata, notes: 'Input requirements\nOutput instructions' })
  assert.equal(saved.updatedAt, original.updatedAt)
  assert.equal(notesEditor.selectedWorkflowId, other.id)
  const edited = await overview.saveWorkflow({ ...stale, name: 'Edited graph', expectedUpdatedAt: stale.updatedAt })
  assert.deepEqual(edited.metadata, saved.metadata)
  const cleared = await notesEditor.updateWorkflowNotes(original.id, ' \n ')
  assert.deepEqual(cleared.metadata, { ...published.metadata, notes: '' })
  await assert.rejects(notesEditor.updateWorkflowNotes(original.id, 'x'.repeat(4001)), /metadata/)
  await notesEditor.deleteWorkflow(original.id)
  await assert.rejects(notesEditor.updateWorkflowNotes(original.id, 'Notes'), /no longer exists/)
})

test('exports use saved notes and preserve edits made while the file is being written', async () => {
  const overview = store()
  await overview.initialize()
  const original = await overview.saveWorkflow({ name: 'Workflow', definition: simple(), metadata: { ...metadata(), notes: 'Earlier notes' } })
  const notesEditor = store()
  await notesEditor.initialize()
  await notesEditor.updateWorkflowNotes(original.id, 'Current notes')
  const published = await overview.exportWorkflow(original.id, { ...original.metadata, version: '2.0.0' }, async (entry, prepared) => {
    assert.equal(prepared.guid, original.metadata.guid)
    assert.equal(prepared.notes, 'Current notes')
    assert.equal(parseWorkflowFile(serializeWorkflowFile(entry.definition, prepared)).metadata.notes, 'Current notes')
    await notesEditor.updateWorkflowNotes(original.id, 'Later notes')
    return true
  })
  assert.equal(published.metadata.guid, original.metadata.guid)
  assert.equal(published.metadata.version, '2.0.0')
  assert.equal(published.metadata.notes, 'Later notes')
})

test('a legacy workflow receives one GUID on its first notes save and retains it through later updates', async () => {
  storage.set(key, JSON.stringify({ workflows: [{ id: 'old', name: 'Legacy', definition: simple(), createdAt: 10, updatedAt: 20 }], selectedWorkflowId: 'old' }))
  const workflow = store()
  await workflow.initialize()
  const first = await workflow.updateWorkflowNotes('old', 'First notes')
  const guid = first.metadata.guid
  const updated = await workflow.updateWorkflowNotes('old', 'Updated notes')
  assert.equal(updated.metadata.guid, guid)
  const edited = await workflow.saveWorkflow({ ...updated, name: 'Edited', expectedUpdatedAt: 20 })
  assert.equal(edited.metadata.guid, guid)
  const published = await workflow.updateWorkflowMetadata('old', { ...edited.metadata, version: '2.0.0' })
  assert.equal(published.metadata.guid, guid)
  const duplicate = await workflow.duplicateWorkflow('old')
  assert.notEqual(duplicate.metadata.guid, guid)
  assert.equal(duplicate.metadata.notes, 'Updated notes')
  await workflow.reload()
  assert.equal(workflow.workflows.find(item => item.id === 'old').metadata.guid, guid)
})

test('description reads use one value without writing or discarding differing legacy content', async () => {
  const header = metadata()
  const cases = [
    { description: 'Earlier description', expected: 'Earlier description', legacy: '' },
    { description: '', metadata: { ...header, notes: 'Current description' }, expected: 'Current description', legacy: '' },
    { description: 'Same content', metadata: { ...header, notes: 'Same content' }, expected: 'Same content', legacy: '' },
    { description: 'Earlier description', metadata: { ...header, notes: 'Current description' }, expected: 'Current description', legacy: 'Earlier description' },
    { description: 'Earlier description', metadata: { ...header, notes: '' }, expected: '', legacy: 'Earlier description' },
  ]
  for (const [index, item] of cases.entries()) {
    const entry = { id: `workflow-${index}`, name: `Workflow ${index}`, definition: simple(), ...item }
    storage.set(key, JSON.stringify({ workflows: [entry], selectedWorkflowId: entry.id }))
    const before = storage.get(key)
    const workflow = store()
    await workflow.initialize()
    assert.equal(resolveWorkflowDescription(workflow.workflows[0]), item.expected)
    assert.equal(legacyWorkflowDescription(workflow.workflows[0]), item.legacy)
    assert.equal(storage.get(key), before)
  }
})

test('saving a unified description preserves old text until it is appended, and old editor snapshots cannot overwrite it', async () => {
  const workflow = store()
  await workflow.initialize()
  const original = await workflow.saveWorkflow({ name: 'Workflow', description: 'Earlier description', definition: simple(), metadata: { ...metadata(), notes: 'Current description' } })
  const stale = JSON.parse(JSON.stringify(original))
  const saved = await workflow.updateWorkflowNotes(original.id, 'Updated description')
  assert.equal(saved.metadata.guid, original.metadata.guid)
  assert.equal(saved.description, 'Updated description')
  assert.equal(saved.metadata.legacyDescription, 'Earlier description')
  assert.equal(legacyWorkflowDescription(saved), 'Earlier description')
  const editor = store()
  await editor.initialize()
  const edited = await editor.saveWorkflow({ ...stale, name: 'Updated graph', expectedUpdatedAt: stale.updatedAt })
  assert.equal(edited.description, saved.description)
  assert.deepEqual(edited.metadata, saved.metadata)
  const cleared = await workflow.updateWorkflowNotes(original.id, '')
  assert.equal(resolveWorkflowDescription(cleared), '')
  assert.equal(cleared.description, '')
  assert.equal(legacyWorkflowDescription(cleared), 'Earlier description')
  const combined = await workflow.updateWorkflowNotes(original.id, 'Updated description\n\nEarlier description')
  assert.equal(combined.metadata.legacyDescription, undefined)
  assert.equal(legacyWorkflowDescription(combined), '')
  assert.equal(combined.description, combined.metadata.notes)
})

test('oversized legacy descriptions and conflicts export losslessly without migrating stored content', async () => {
  const longText = '完整说明\n'.repeat(1200)
  for (const notes of [undefined, 'Current description']) {
    const entry = { id: 'legacy', name: 'Legacy', description: longText.trim(), definition: simple(), metadata: { ...metadata(), ...(notes === undefined ? {} : { notes }) }, createdAt: 10, updatedAt: 20 }
    storage.set(key, JSON.stringify({ workflows: [entry], selectedWorkflowId: entry.id }))
    const workflow = store()
    await workflow.initialize()
    const before = JSON.parse(storage.get(key)).workflows[0]
    let file
    await workflow.exportWorkflow(entry.id, entry.metadata, async (current, prepared) => {
      file = parseWorkflowFile(serializeWorkflowFile(current.definition, prepared))
      return true
    })
    assert.deepEqual(JSON.parse(storage.get(key)).workflows[0].metadata, before.metadata)
    assert.equal(file.metadata.notes, notes ?? longText.trim())
    assert.equal(file.metadata.legacyDescription, notes === undefined ? undefined : longText.trim())
    const imported = await workflow.saveWorkflow({ name: 'Imported', ...file })
    assert.equal(resolveWorkflowDescription(imported), notes ?? longText.trim())
    assert.equal(legacyWorkflowDescription(imported), notes === undefined ? '' : longText.trim())
    await assert.rejects(workflow.updateWorkflowNotes(imported.id, longText), /metadata/)
  }
})
