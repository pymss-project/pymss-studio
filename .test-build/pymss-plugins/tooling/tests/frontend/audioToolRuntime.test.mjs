import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, inject, reactive, ref, watch } from 'vue'
import { parse } from 'vue/compiler-sfc'

const source = ts.createSourceFile('runtime.ts', readFileSync(new URL('../../frontend/src/features/audio-tools/runtime.ts', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true)
const code = ts.transpileModule(source.statements
  .filter(statement => !ts.isImportDeclaration(statement))
  .map(statement => statement.getText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText
const locale = {}
for (const plugin of readdirSync(new URL('../../../plugins/', import.meta.url), { withFileTypes: true })) {
  if (!plugin.isDirectory() || plugin.name.startsWith('.')) continue
  const messages = JSON.parse(readFileSync(new URL(`../../../plugins/${plugin.name}/ui/src/locales/zh-CN.json`, import.meta.url), 'utf8'))
  for (const [group, values] of Object.entries(messages)) locale[group] = { ...locale[group], ...values }
}
const flush = () => new Promise(setImmediate)

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

async function createRuntime(t, { autoStart = true, listenGate, launchGate } = {}) {
  const exports = {}
  const guards = [], messages = [], calls = []
  let receive
  let cancelCommand = async () => true
  vm.runInNewContext(code, {
    exports, computed, inject, reactive, ref, setInterval, clearInterval,
    window: { __TAURI_INTERNALS__: {} },
    isStudioHost: () => true,
    useI18n: () => ({ t: (key, values = {}) => (key.split('.').reduce((value, segment) => value?.[segment], locale) || key).replace(/\{(\w+)\}/g, (match, name) => values[name] ?? match) }),
    useMessage: () => Object.fromEntries(['success', 'warning', 'error', 'info'].map(level => [level, text => messages.push({ level, text })])),
    registerWindowCloseGuard: guard => guards.push(guard),
    listen: async (_, handler) => { receive = handler; if (listenGate) await listenGate.promise; return () => {} },
    invoke: async (command, args) => {
      calls.push({ command, args })
      if (command === 'cancel_task') return cancelCommand()
      if (launchGate) await launchGate.promise
      return { started: true, taskId: args.payload.requestId }
    },
  })
  const runtime = exports.createAudioToolRuntime()
  if (autoStart) await runtime.start()
  function emit(type, payload = {}) {
    const requestId = calls.findLast(call => call.command.startsWith('start_'))?.args.payload.requestId
    receive?.({ payload: { type, requestId, payload } })
  }
  t.after(async () => {
    listenGate?.resolve()
    launchGate?.resolve()
    await flush()
    emit('task_cancelled')
    await flush()
  })
  return { runtime, guards, messages, calls, emit, setCancel: fn => { cancelCommand = fn } }
}

test('closing during listener initialization cancels without dispatching a backend task', async t => {
  const listenGate = deferred()
  const { runtime, guards, calls } = await createRuntime(t, { autoStart: false, listenGate })
  const work = runtime.execute('midi', { action: 'detect_bpm', inputs: ['audio.wav'] })
  await flush()
  assert.equal(runtime.stateFor('midi').value.busy, true)
  await guards[0]()
  assert.equal(await work, null)
  assert.equal(runtime.stateFor('midi').value.progress.phase, 'cancelled')
  assert.equal(runtime.stateFor('midi').value.anyBusy, false)
  listenGate.resolve()
  await flush()
  assert.equal(calls.length, 0)
})

test('cancellation waits for backend registration and cleanup before clearing busy state', async t => {
  const launchGate = deferred()
  const { runtime, calls, emit } = await createRuntime(t, { launchGate })
  const work = runtime.execute('tempo_key', { inputs: ['audio.wav'] })
  await flush()
  let cancelled = false
  const cancellation = runtime.cancel().then(accepted => { cancelled = accepted })
  const repeated = runtime.cancel()
  await flush()
  assert.equal(calls.filter(call => call.command === 'cancel_task').length, 0)
  assert.equal(cancelled, false)
  assert.equal(runtime.stateFor('tempo_key').value.busy, true)
  launchGate.resolve()
  await flush()
  assert.equal(calls.filter(call => call.command === 'cancel_task').length, 1)
  assert.equal(cancelled, false)
  emit('task_cancelled')
  await cancellation
  assert.equal(await repeated, true)
  assert.equal(await work, null)
  assert.equal(cancelled, true)
  assert.equal(runtime.stateFor('tempo_key').value.busy, false)
})

test('a startup failure during cancellation settles both callers without a cancel command', async t => {
  const launchGate = deferred()
  const { runtime, calls } = await createRuntime(t, { launchGate })
  const work = runtime.execute('tempo_key', { inputs: ['audio.wav'] })
  await flush()
  const cancellation = runtime.cancel()
  launchGate.reject(new Error('Backend startup failed'))
  assert.equal(await work, null)
  assert.equal(await cancellation, false)
  assert.match(runtime.stateFor('tempo_key').value.error, /Backend startup failed/)
  assert.equal(runtime.stateFor('tempo_key').value.anyBusy, false)
  assert.equal(calls.filter(call => call.command === 'cancel_task').length, 0)
})

test('a completed task awaiting startup acknowledgement is not cancelled again', async t => {
  const launchGate = deferred()
  const { runtime, calls, emit } = await createRuntime(t, { launchGate })
  const work = runtime.execute('tempo_key', { inputs: ['audio.wav'] })
  await flush()
  const cancellation = runtime.cancel()
  const result = { operation: 'tempo_key', items: [], succeeded: 1 }
  emit('audio_tool_result', result)
  launchGate.resolve()
  assert.equal(await work, result)
  assert.equal(await cancellation, false)
  assert.equal(calls.filter(call => call.command === 'cancel_task').length, 0)
})

test('listener setup failure settles the task without dispatching work', async t => {
  const listenGate = deferred()
  const { runtime, calls } = await createRuntime(t, { autoStart: false, listenGate })
  const work = runtime.execute('tempo_key', { inputs: ['audio.wav'] })
  await flush()
  listenGate.reject(new Error('Event listener setup failed'))
  assert.equal(await work, null)
  assert.equal(await runtime.cancel(), false)
  assert.equal(runtime.stateFor('tempo_key').value.anyBusy, false)
  assert.match(runtime.stateFor('tempo_key').value.error, /Event listener setup failed/)
  assert.equal(calls.length, 0)
})

test('repeated cancellation shares failures and allows a retry', async t => {
  const { runtime, calls, emit, setCancel } = await createRuntime(t)
  const work = runtime.execute('tempo_key', { inputs: ['audio.wav'] })
  await flush()
  setCancel(async () => { throw new Error('Cancellation request failed') })
  const results = await Promise.allSettled([runtime.cancel(), runtime.cancel()])
  assert.equal(calls.filter(call => call.command === 'cancel_task').length, 1)
  for (const result of results) {
    assert.equal(result.status, 'rejected')
    assert.match(result.reason.message, /Cancellation request failed/)
  }
  assert.equal(runtime.stateFor('tempo_key').value.busy, true)
  assert.equal(runtime.stateFor('tempo_key').value.cancelling, false)
  setCancel(async () => true)
  const retry = runtime.cancel()
  await flush()
  emit('task_cancelled')
  assert.equal(await retry, true)
  assert.equal(await work, null)
})

function createMidi(runtime) {
  const { descriptor } = parse(readFileSync(new URL('../../../plugins/game/ui/src/MidiTool.vue', import.meta.url), 'utf8'))
  const script = ts.createSourceFile('midi.ts', descriptor.scriptSetup.content, ts.ScriptTarget.Latest, true)
  const scriptCode = ts.transpileModule(script.statements.filter(statement => !ts.isImportDeclaration(statement))
    .map(statement => statement.getText(script)).join('\n'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  return vm.runInNewContext(`${scriptCode}\n({ inputPath, bpm, detectingBpm, detectBpm })`, {
    computed, ref, watch, toolRef: (_, initial) => ref(initial), onMounted: () => {},
    useAudioToolRuntime: () => runtime,
    useSettingsStore: () => ({ outputDir: '' }),
    useI18n: () => ({ t: key => key }),
    useMessage: () => ({ success: () => {}, error: () => {} }),
    errorMessage: error => String(error),
    invoke: () => { throw new Error('BPM detection must use the audio tool runtime') },
  })
}

test('MIDI BPM detection shares busy state and waits for cancellation before closing', async t => {
  const { runtime, guards, calls, emit } = await createRuntime(t)
  const midi = createMidi(runtime)
  midi.inputPath.value = 'vocals.wav'
  const detecting = midi.detectBpm()
  await flush()
  assert.equal(runtime.stateFor('midi').value.busy, true)
  assert.equal(runtime.stateFor('convert').value.anyBusy, true)
  assert.equal(await runtime.execute('convert', { inputs: ['audio.wav'] }), null)
  assert.equal(calls[0].command, 'start_tempo_key_analysis')
  assert.equal(calls[0].args.payload.action, 'detect_bpm')
  let closed = false
  const closing = guards[0]().then(() => { closed = true })
  await flush()
  assert.equal(closed, false)
  emit('task_cancelled')
  await Promise.all([detecting, closing])
  assert.equal(midi.bpm.value, 120)
  assert.equal(midi.detectingBpm.value, false)
  assert.equal(runtime.stateFor('midi').value.progress.phase, 'cancelled')
})

test('MIDI BPM detection updates tempo and leaves transcription on its Python entry point', async t => {
  const { runtime, calls, emit, messages } = await createRuntime(t)
  const midi = createMidi(runtime)
  midi.inputPath.value = 'vocals.wav'
  const detecting = midi.detectBpm()
  await flush()
  emit('audio_tool_result', { operation: 'midi', action: 'detect_bpm', bpm: 128.2, roundedBpm: 128, succeeded: 1 })
  await detecting
  assert.equal(midi.bpm.value, 128)
  assert.equal(messages.at(-1).text, '已检测到速度：128 BPM')
  const conversion = runtime.execute('midi', { inputPath: 'vocals.wav', bpm: 128 })
  await flush()
  assert.equal(calls.at(-1).command, 'start_audio_tool')
  emit('audio_tool_result', { operation: 'midi', bpm: 128, noteCount: 12 })
  assert.equal((await conversion).noteCount, 12)
})

test('failed MIDI BPM detection preserves manual tempo', async t => {
  const { runtime, emit } = await createRuntime(t)
  const midi = createMidi(runtime)
  midi.inputPath.value = 'vocals.wav'
  midi.bpm.value = 137
  const detecting = midi.detectBpm()
  await flush()
  emit('error', { message: 'No rhythmic onsets were detected in the audio' })
  await detecting
  assert.equal(midi.bpm.value, 137)
  assert.match(runtime.stateFor('midi').value.error, /No rhythmic onsets/)
})

test('cancellation and window close wait for native work to release resources', async t => {
  const { runtime, guards, emit } = await createRuntime(t)
  const work = runtime.execute('tempo_key', { inputs: ['audio.wav'] })
  await flush()
  let closed = false
  const closing = guards[0]().then(() => { closed = true })
  await flush()
  assert.equal(closed, false)
  assert.equal(runtime.stateFor('tempo_key').value.cancelling, true)
  assert.equal(runtime.stateFor('tempo_key').value.busy, true)
  let secondCancellationFinished = false
  const secondCancellation = runtime.cancel().then(() => { secondCancellationFinished = true })
  await flush()
  assert.equal(secondCancellationFinished, false)
  emit('task_cancelled')
  await Promise.all([closing, secondCancellation])
  assert.equal(await work, null)
  assert.equal(closed, true)
  assert.equal(runtime.stateFor('tempo_key').value.busy, false)
  assert.equal(runtime.stateFor('tempo_key').value.progress.phase, 'cancelled')
})

test('a result arriving during cancellation remains a successful result', async t => {
  const { runtime, emit, setCancel } = await createRuntime(t)
  const result = { operation: 'tempo_key', items: [], succeeded: 1 }
  const work = runtime.execute('tempo_key', { inputs: ['audio.wav'] })
  await flush()
  setCancel(async () => { emit('audio_tool_result', result); return false })
  assert.equal(await runtime.cancel(), false)
  assert.equal(await work, result)
  assert.equal(runtime.stateFor('tempo_key').value.error, '')
  assert.equal(runtime.stateFor('tempo_key').value.progress.phase, 'completed')
})

test('loudness target deviations keep measurements and produce a localized warning', async t => {
  const { runtime, messages, emit } = await createRuntime(t)
  const work = runtime.execute('loudness', { inputs: ['audio.wav'] })
  await flush()
  const result = {
    operation: 'loudness', measurement: 'rms', target: -5, succeeded: 1,
    warnings: ['loudness_target_not_reached'],
    measurements: [{ path: 'audio_loudness.wav', inputLevel: -12, outputLevel: -12.2, target: -5, unit: 'dB', withinTolerance: false }],
  }
  emit('audio_tool_result', result)
  assert.equal(await work, result)
  assert.equal(runtime.stateFor('loudness').value.result.measurements[0].outputLevel, -12.2)
  assert.deepEqual(messages, [{ level: 'warning', text: locale.tools.loudnessTargetNotReached }])
  assert.equal(runtime.stateFor('loudness').value.logs.at(-1).detail, locale.tools.loudnessTargetNotReached)
})

test('true peak limit warnings remain visible when loudness is within tolerance', async t => {
  const { runtime, messages, emit } = await createRuntime(t)
  const work = runtime.execute('loudness', { inputs: ['square.mp3'] })
  await flush()
  const result = {
    operation: 'loudness', measurement: 'rms', target: -5, succeeded: 1,
    warnings: ['loudness_true_peak_exceeded'],
    measurements: [{ path: 'square_loudness.mp3', inputLevel: -6.5, outputLevel: -6.3, target: -5, unit: 'dB',
      withinTolerance: true, outputTruePeak: -2.66, maxTruePeak: -3, withinTruePeakLimit: false }],
  }
  emit('audio_tool_result', result)
  assert.equal(await work, result)
  assert.equal(runtime.stateFor('loudness').value.result.measurements[0].outputTruePeak, -2.66)
  assert.deepEqual(messages, [{ level: 'warning', text: locale.tools.loudnessTruePeakExceeded }])
  assert.equal(runtime.stateFor('loudness').value.logs.at(-1).detail, locale.tools.loudnessTruePeakExceeded)
})

test('merge directory alias rejections show the localized error', async t => {
  const { runtime, emit } = await createRuntime(t)
  const work = runtime.execute('merge', { inputDir: 'input', outputDir: 'alias' })
  await flush()
  emit('error', { code: 'MERGE_SAME_DIRECTORY', message: 'Input and output directories must be different' })
  assert.equal(await work, null)
  assert.equal(runtime.stateFor('merge').value.error, locale.tools.mergeSameDirectory)
})
