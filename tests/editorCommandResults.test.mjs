import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { isAudioOutput } from '../src/utils/stemOutputs.ts'

const source = ts.createSourceFile('editor.ts', readFileSync(new URL('../src/stores/editor.ts', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true)
let declaration
function find(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'requestProjectFromTask') declaration = node
  ts.forEachChild(node, find)
}
find(source)
const code = ts.transpileModule(declaration.getText(source), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function environment() {
  const calls = []
  const request = vm.runInNewContext(`${code}\nrequestProjectFromTask`, {
    invoke: async (...args) => { calls.push(JSON.parse(JSON.stringify(args))); return { id: 'audio' } },
    i18n: { global: { t: key => key } },
    isAudioOutput,
  })
  return { calls, request }
}

test('mixed command and audio results send only audio tracks to project creation', async () => {
  const { request, calls } = environment()
  const outputs = [{ path: '/out/voice.wav', stem: 'voice' }, { path: '/out/captions.srt', stem: 'captions', kind: 'file' },
    { path: '/out/native.wav', stem: 'native', kind: 'file', mediaType: 'audio/wav' }]
  await request({ id: 'task', input: '/in/audio.wav', output: '/out', outputs })
  assert.deepEqual(calls[0][1].payload.outputs, [outputs[0], outputs[2]])
  assert.equal(outputs.length, 3)
})

test('file-only results never create an audio project or scan unrelated output files', async () => {
  const { request, calls } = environment()
  await assert.rejects(request({ id: 'task', input: '', output: '/out', outputs: [{ path: '/out/document.txt', kind: 'file' }] }), /editor.importEmpty/)
  assert.equal(calls.length, 0)
})

test('legacy empty metadata retains the existing output directory recovery path', async () => {
  const { request, calls } = environment()
  await request({ id: 'legacy', input: '/in/audio.wav', output: '/out', outputs: [] })
  assert.deepEqual(calls[0][1].payload.outputs, [])
  assert.equal(calls[0][1].payload.outputDir, '/out')
})
