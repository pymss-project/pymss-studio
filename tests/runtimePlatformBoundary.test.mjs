import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { detectRuntimePlatform } from '../src/utils/runtime.ts'

function componentFunction(file, name, scope) {
  const source = readFileSync(new URL(file, import.meta.url), 'utf8').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  const parsed = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const declaration = parsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === name)
  assert.ok(declaration, `${name} must exist`)
  const code = ts.transpileModule(declaration.getText(parsed), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  return vm.runInNewContext(`${code}\n${name}`, scope)
}

function componentComputed(file, name, scope) {
  const source = readFileSync(new URL(file, import.meta.url), 'utf8').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  const parsed = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const declaration = parsed.statements
    .filter(ts.isVariableStatement)
    .flatMap(node => [...node.declarationList.declarations])
    .find(node => node.name.getText(parsed) === name)
  assert.ok(declaration?.initializer, `${name} must exist`)
  const code = ts.transpileModule(`const ${name} = ${declaration.initializer.getText(parsed)}`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  return vm.runInNewContext(`${code}\n${name}`, { computed: fn => fn(), ...scope })
}

test('deferred update preflight keeps busy feedback visible', async () => {
  const scope = {
    deferredUpdateInstalling: { value: false }, deferredUpdateError: { value: '' }, deferredUpdateModalVisible: { value: true },
    updates: { installationBlockedReason: 'busy', downloadAndInstall() { assert.fail('busy update must not start') } },
  }
  await componentFunction('../src/App.vue', 'installDeferredUpdate', scope)()
  assert.equal(scope.deferredUpdateModalVisible.value, true)
  assert.equal(scope.deferredUpdateError.value, 'busy')
})

test('deferred early errors reopen the prompt when no install overlay owns the error', async () => {
  for (const overlay of [false, true]) {
    const scope = {
      deferredUpdateInstalling: { value: false }, deferredUpdateError: { value: '' }, deferredUpdateModalVisible: { value: true },
      updates: { installationBlockedReason: '', isInstallOverlayVisible: overlay, downloadAndInstall: async () => { throw new Error('update unavailable') } },
    }
    await componentFunction('../src/App.vue', 'installDeferredUpdate', scope)()
    assert.equal(scope.deferredUpdateModalVisible.value, !overlay)
    assert.match(scope.deferredUpdateError.value, /update unavailable/)
  }
})

for (const [platform, machine, expected] of [
  ['darwin', 'arm64', ['mlx']],
  ['darwin', 'aarch64', ['mlx']],
  ['darwin', 'x86_64', ['mlx']],
  ['darwin', '', ['mlx']],
  ['win32', 'AMD64', ['cpu', 'cuda', 'rocm']],
  ['linux', 'x86_64', ['cpu', 'cuda', 'rocm']],
]) {
  test(`${platform}/${machine} settings and onboarding offer the intended runtime environments`, () => {
    const scope = {
      app: { runtimeInfo: { platform, machine } },
      runtimePlatform: { value: detectRuntimePlatform({ platform, machine }) },
      runtimeBackendName: backend => backend,
      runtimeBackendDescription: backend => backend,
      t: key => key,
      backendInstalled: () => true,
      runtimeRecommendedBackend: { value: expected[0] },
      runtimeSizeHint: () => '',
    }
    const catalog = componentComputed('../src/views/SettingsView.vue', 'runtimeBackendCatalog', scope)
    const choices = componentComputed('../src/components/StartupOnboarding.vue', 'runtimeBackendChoices', scope)
    assert.deepEqual(Array.from(catalog, item => item.backend), expected)
    assert.deepEqual(Array.from(choices, item => item.value), expected)
  })
}

test('onboarding offers only MLX on Intel and unidentified Macs', () => {
  for (const machine of ['x86_64', '']) {
    const choices = componentComputed('../src/components/StartupOnboarding.vue', 'runtimeBackendChoices', {
      runtimePlatform: { value: detectRuntimePlatform({ platform: 'darwin', machine }) },
      runtimeBackendName: backend => backend, t: key => key, backendInstalled: () => false,
      runtimeRecommendedBackend: { value: 'mlx' }, runtimeSizeHint: () => '',
    })
    assert.deepEqual(Array.from(choices, item => item.value), ['mlx'])
  }
})

for (const platform of ['darwin', 'linux', 'win32']) {
  for (const installed of [false, true]) {
    test(`${platform} onboarding hands ${installed ? 'an installed' : 'a missing'} runtime to management`, async () => {
      const actions = []
      const app = {
        runtimeManagementSupported: platform === 'win32' || platform === 'linux',
        runtimeReadyForBackend: () => false,
        runtimeInfo: { installedEnvironments: [{ backend: 'cpu', pythonPath: '/runtime/cpu/bin/python' }] },
        activateRuntime: async () => actions.push('activate'),
        installRuntime: async () => actions.push('install'),
      }
      const primary = componentFunction('../src/components/StartupOnboarding.vue', 'primaryAction', {
        app,
        currentStep: { value: 'runtime' }, canGoNext: { value: true },
        selectedBackendReady: { value: false },
        finishOnboarding: async (_event, skip, manage) => actions.push({ skip, manage }),
      })
      await primary({})
      assert.deepEqual(actions, [{ skip: true, manage: true }])
    })
  }
}

test('unsupported platforms skip core update prompts', () => {
  const actions = []
  const scope = { app: { runtimeCoreUpdateSupported: false, updateRuntimeCore: async () => actions.push('update') }, runtimeCorePromptVisible: { value: false } }
  componentFunction('../src/App.vue', 'showRuntimeCorePrompt', scope)()
  assert.deepEqual(actions, [])
})

for (const backend of ['mlx', 'cpu']) {
  test(`${backend} startup requests confirmation and never starts a core synchronization`, () => {
    const actions = []
    const scope = {
      app: { runtimeManagementSupported: false, runtimeCoreUpdateSupported: true, updateRuntimeCore: async (...args) => actions.push(args) },
      runtimeCorePromptShown: { value: false },
      bootReady: { value: true },
      showStartupOnboarding: { value: false },
      isStandaloneRoute: { value: false },
      runtimeManifestSyncRequired: { value: true },
      runtimeCoreUpdateAvailable: { value: true },
      runtimeCorePromptBlocked: { value: false },
      activeRuntime: { value: { backend, pythonPath: '/runtime/bin/python3' } },
      runtimeCorePromptVisible: { value: false },
      beginnerGuide: { visible: false },
      settings: { beginnerGuideInvitationSeen: true },
    }
    componentFunction('../src/App.vue', 'showRuntimeCorePrompt', scope)()
    assert.equal(actions.length, 0)
    assert.equal(scope.runtimeCorePromptVisible.value, true)
  })
}

test('runtime update prompts wait until the beginner guide is dismissed', () => {
  for (const [visible, invitationSeen] of [[true, true], [false, false]]) {
    const scope = {
      app: { runtimeCoreUpdateSupported: true },
      runtimeCorePromptShown: { value: false }, runtimeCorePromptVisible: { value: false },
      runtimeCorePromptBlocked: { value: false }, runtimeCoreUpdateAvailable: { value: true },
      bootReady: { value: true }, showStartupOnboarding: { value: false }, isStandaloneRoute: { value: false },
      beginnerGuide: { visible }, settings: { beginnerGuideInvitationSeen: invitationSeen },
    }
    componentFunction('../src/App.vue', 'showRuntimeCorePrompt', scope)()
    assert.equal(scope.runtimeCorePromptVisible.value, false)
    assert.equal(scope.runtimeCorePromptShown.value, false)
  }
})

test('runtime notices wait for updates and reads, and close once the installed core is current', () => {
  for (const status of ['preparing', 'prepared', 'updating']) {
    const blocked = componentComputed('../src/App.vue', 'runtimeCorePromptBlocked', {
      app: { runtimeCoreUpdateStatus: status, runtimeInfoLoading: false, runtimeCoreVersionsLoading: false },
    })
    assert.equal(blocked, true)
  }
  for (const loading of ['runtimeInfoLoading', 'runtimeCoreVersionsLoading']) {
    assert.equal(componentComputed('../src/App.vue', 'runtimeCorePromptBlocked', {
      app: { runtimeCoreUpdateStatus: 'success', [loading]: true },
    }), true)
  }
  for (const [blocked, available] of [[true, true], [false, false]]) {
    const scope = {
      app: { runtimeCoreUpdateSupported: true },
      runtimeCorePromptShown: { value: true }, runtimeCorePromptVisible: { value: true },
      runtimeCorePromptBlocked: { value: blocked }, runtimeCoreUpdateAvailable: { value: available },
      bootReady: { value: true }, showStartupOnboarding: { value: false }, isStandaloneRoute: { value: false },
    }
    componentFunction('../src/App.vue', 'showRuntimeCorePrompt', scope)()
    assert.equal(scope.runtimeCorePromptVisible.value, false)
  }
})

for (const confirmed of [false, true]) {
  test(`settings ${confirmed ? 'applies' : 'discards'} the exact prepared plan after confirmation`, async () => {
    const calls = []
    const plan = { id: 'confirmed-plan', backend: 'mlx', pythonPath: '/runtime/bin/python', mode: 'update', mirror: 'pypi', channel: 'prerelease', currentVersions: { pymss: '2.1.8', 'pymss-core': '0.1.11' }, targetVersions: { pymss: '2.2.0rc1', 'pymss-core': '0.2.0' }, dependencyCount: 1 }
    const scope = {
      app: {
        runtimeCoreUpdateSupported: true,
        runtimeCoreUpdateStatus: 'prepared',
        prepareRuntimeCoreUpdate: async () => { calls.push('prepare'); return plan },
        updateRuntimeCore: async (...args) => calls.push(args),
        discardRuntimeCorePlan: id => { assert.equal(id, plan.id); scope.app.runtimeCoreUpdateStatus = 'idle' },
      },
      runtimeBusy: { value: false }, runtimeMirror: { value: 'auto' }, currentLocale: { value: 'zh-CN' },
      runtimeCoreActionPending: { value: false },
      t: (key, values) => `${key}:${JSON.stringify(values || {})}`,
      confirmRuntimeAction: async (_title, content) => { assert.match(content, /2\.2\.0rc1/); return confirmed },
      message: { error: error => assert.fail(error) },
    }
    await componentFunction('../src/views/SettingsView.vue', 'runRuntimeCoreOperation', scope)({ backend: 'mlx' }, 'update')
    assert.equal(calls[0], 'prepare')
    assert.equal(calls.length, confirmed ? 2 : 1)
    assert.equal(scope.runtimeCoreActionPending.value, false)
    if (confirmed) assert.equal(calls[1][3].planId, plan.id)
    else assert.equal(scope.app.runtimeCoreUpdateStatus, 'idle')
  })
}

test('every confirmation dismissal settles the pending runtime operation', async () => {
  for (const event of ['onMaskClick', 'onEsc', 'onClose', 'onNegativeClick', 'onPositiveClick']) {
    let options
    const confirm = componentFunction('../src/views/SettingsView.vue', 'confirmRuntimeAction', {
      dialog: { warning: value => { options = value } }, t: key => key,
    })
    const result = confirm('Update core', 'Version plan', 'Confirm')
    assert.equal(typeof options[event], 'function', `${event} must settle the confirmation`)
    options[event]()
    assert.equal(await result, event === 'onPositiveClick')
  }
})

test('core confirmation keeps its plan exclusive until the confirmed apply is dispatched', async () => {
  let confirm
  let dispatched
  const confirmation = new Promise(resolve => { confirm = resolve })
  const applying = new Promise(resolve => { dispatched = resolve })
  const plan = { id: 'first-plan', backend: 'cuda', pythonPath: '/runtime/python', mode: 'update', mirror: 'pypi', channel: 'stable', currentVersions: { pymss: '2.1.8' }, targetVersions: { pymss: '2.1.9' }, dependencyCount: 0 }
  const calls = []
  const scope = {
    app: {
      runtimeCoreUpdateSupported: true,
      prepareRuntimeCoreUpdate: async () => { calls.push('prepare'); return plan },
      updateRuntimeCore: async (...args) => { calls.push(args); await applying },
    },
    runtimeBusy: { value: false }, runtimeCoreActionPending: { value: false },
    runtimeMirror: { value: 'auto' }, currentLocale: { value: 'zh-CN' },
    confirmRuntimeAction: () => confirmation, t: key => key,
    message: { error: error => assert.fail(error) },
  }
  const run = componentFunction('../src/views/SettingsView.vue', 'runRuntimeCoreOperation', scope)
  const first = run({ backend: 'cuda' }, 'update')
  await Promise.resolve()
  const duplicate = run({ backend: 'cuda' }, 'update')
  await Promise.resolve()
  assert.equal(calls.length, 1, 'A second click must not replace the plan awaiting confirmation')
  await duplicate
  confirm(true)
  await Promise.resolve()
  const duringApply = run({ backend: 'cuda' }, 'update')
  await Promise.resolve()
  assert.equal(calls.length, 2, 'A second plan must not start before apply acknowledgement')
  await duringApply
  assert.equal(calls[1][3].planId, 'first-plan')
  dispatched()
  await first
  assert.equal(scope.runtimeCoreActionPending.value, false)
})

test('non-Windows developer controls cannot open an editor or mutate runtime files', async () => {
  const scope = { app: { runtimeManagementSupported: true, runtimeDebugSupported: false } }
  for (const name of ['openRuntimeDebugEditor', 'saveRuntimeDebugEditor', 'restoreRuntimeDebugFile', 'overrideActiveRuntimePointer']) {
    await componentFunction('../src/views/DebugView.vue', name, scope)({ editable: true, backupExists: true })
  }
})
