import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { detectRuntimePlatform, runtimeInstallOptionsFromPlan } from '../src/utils/runtime.ts'
import { runtimeErrorSummary, runtimeOperationError } from '../src/utils/runtimeErrors.ts'

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

test('official macOS release exposes application maintenance using its published target', () => {
  const workflow = readFileSync(new URL('../.github/workflows/release-macos.yml', import.meta.url), 'utf8')
  const target = workflow.match(/PYMSS_BUILD_TARGET:\s*([^\s]+)/)?.[1]
  assert.ok(target)
  const visible = componentComputed('../src/views/SettingsView.vue', 'applicationMaintenanceVisible', {
    updateSupported: { value: true }, buildInfo: { value: { target } },
  })
  assert.equal(visible, true)
})

test('unsupported builds do not expose macOS application maintenance', () => {
  for (const [target, supported] of [['macos-arm64', false], ['windows-x64', true], ['linux-x64', true], ['dev', false]]) {
    const visible = componentComputed('../src/views/SettingsView.vue', 'applicationMaintenanceVisible', {
      updateSupported: { value: supported }, buildInfo: { value: { target } },
    })
    assert.equal(visible, false, `${target}/${supported}`)
  }
})

test('manual upgrade opens its release even when an earlier repair failed', async () => {
  const opened = []
  const url = 'https://github.com/pymss-project/pymss-studio/releases/tag/v0.0.17'
  const scope = {
    updates: { repairing: true, manualInstallUrl: url }, app: { buildInfoVersion: '0.0.16' },
    manualUpdateError: { value: 'previous failure' }, manualUpdateModalVisible: { value: true },
    open: async value => opened.push(value),
  }
  await componentFunction('../src/App.vue', 'openManualUpdate', scope)()
  assert.deepEqual(opened, [url])
  assert.equal(scope.manualUpdateError.value, '')
  assert.equal(scope.manualUpdateModalVisible.value, false)
})

test('repair failure opens the current release instead of an offered upgrade', async () => {
  const opened = []
  const scope = {
    updates: { repairing: true, manualInstallUrl: 'https://github.com/pymss-project/pymss-studio/releases/tag/v0.0.17' },
    app: { buildInfoVersion: '0.0.16' }, manualUpdateError: { value: '' }, manualUpdateModalVisible: { value: false },
    open: async value => opened.push(value),
  }
  await componentFunction('../src/App.vue', 'openManualUpdate', scope)(true)
  assert.deepEqual(opened, ['https://github.com/pymss-project/pymss-studio/releases/tag/v0.0.16'])
})

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

test('runtime version discovery is ready without a separate pymss-core release', () => {
  for (const loading of [false, true]) {
    const ready = componentComputed('../src/views/SettingsView.vue', 'runtimeCoreVersionCheckReady', {
      app: { runtimeCoreVersionsLoading: loading },
      latestPymssVersion: { value: '2.3.5' },
      latestPymssCoreVersion: { value: null },
    })
    assert.equal(ready, !loading)
  }
  assert.equal(componentComputed('../src/views/SettingsView.vue', 'runtimeCoreVersionCheckReady', {
    app: { runtimeCoreVersionsLoading: false },
    latestPymssVersion: { value: null }, latestPymssCoreVersion: { value: '0.1.15' },
  }), false)
})

test('startup update prompts announce pymss without promising an independent core upgrade', () => {
  const prompt = componentComputed('../src/App.vue', 'runtimeCorePromptContent', {
    runtimeManifestSyncRequired: { value: false },
    activeRuntime: { value: { backend: 'cuda', pymssVersion: '2.3.4', pymssCoreVersion: '0.1.14' } },
    app: { runtimeCoreVersions: { packages: {
      pymss: { latestVersion: '2.3.5' }, 'pymss-core': { latestVersion: '0.1.15' },
    } } },
    runtimeBackendLabel: value => value,
    t: (key, values) => ({ key, values }),
  })
  assert.equal(prompt.key, 'settings.runtimeCoreStartupPrompt')
  assert.equal(prompt.values.pymss, '2.3.4')
  assert.equal(prompt.values.latest, '2.3.5')
  assert.equal('coreLatest' in prompt.values, false)
  for (const locale of ['en', 'zh-CN']) {
    const messages = JSON.parse(readFileSync(new URL(`../src/i18n/${locale}.json`, import.meta.url), 'utf8'))
    assert.equal(messages.settings.runtimeCoreStartupPrompt.includes('{coreLatest}'), false)
    assert.equal(messages.settings.runtimeCoreUpdateAvailable.includes('{core}'), false)
  }
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

for (const locale of ['zh-CN', 'en']) {
  test(`settings shows a localized ${locale} summary when plan preparation rejects`, async () => {
    const translations = JSON.parse(readFileSync(new URL(`../src/i18n/${locale}.json`, import.meta.url), 'utf8')).settings
    const failure = runtimeOperationError({ code: 'RUNTIME_MANIFEST_INCOMPATIBLE', message: 'Raw worker incompatibility error' })
    const errors = []
    const scope = {
      app: { runtimeCoreUpdateSupported: true, runtimeCoreUpdateStatus: 'error', prepareRuntimeCoreUpdate: async () => { throw failure } },
      runtimeBusy: { value: false }, runtimeCoreActionPending: { value: false },
      runtimeMirror: { value: 'auto' }, currentLocale: { value: locale },
      runtimeErrorSummary,
      t: key => translations[key.replace('settings.', '')],
      message: { error: text => errors.push(text) },
    }
    await componentFunction('../src/views/SettingsView.vue', 'runRuntimeCoreOperation', scope)({ backend: 'cuda' }, 'update')
    assert.deepEqual(errors, [translations.runtimeCoreManifestIncompatible])
    assert.equal(scope.runtimeCoreActionPending.value, false)
    scope.app.runtimeCoreUpdateStatus = 'cancelled'
    errors.length = 0
    await componentFunction('../src/views/SettingsView.vue', 'runRuntimeCoreOperation', scope)({ backend: 'cuda' }, 'update')
    assert.deepEqual(errors, [])
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

for (const confirmed of [false, true]) {
  test(`ROCm plan ${confirmed ? 'authorizes' : 'declines'} replacement of the saved previous version`, async () => {
    const calls = []
    const plan = { backend: 'rocm', action: 'upgrade', profileId: 'next', requestedTargets: ['gfx1100'], installationEnabled: true,
      requiresPreviousReplacement: true, previousProfile: { id: 'old-profile', packages: { torch: '2.9.1' } },
      expectedProfileId: 'old', expectedProfileRevision: 1, expectedBaseFingerprint: 'base', expectedTargetFingerprint: 'target' }
    const scope = {
      app: { installRuntime: async (...args) => calls.push(args) }, rocmPlan: { value: plan }, rocmPlanReady: { value: true },
      runtimeBusy: { value: false }, runtimeMirror: { value: 'auto' }, currentLocale: { value: 'zh-CN' }, rocmDialogVisible: { value: true },
      runtimeInstallPlanMatches: () => true, rocmInstallOptions: () => ({}), runtimeInstallOptionsFromPlan,
      t: (key, values) => `${key}:${JSON.stringify(values || {})}`,
      confirmRuntimeAction: async (_title, content) => { assert.match(content, /2\.9\.1/); return confirmed },
      rocmActionError: { value: '' }, runtimeErrorSummary: value => String(value),
    }
    await componentFunction('../src/views/SettingsView.vue', 'confirmRocmInstall', scope)()
    assert.equal(calls.length, confirmed ? 1 : 0)
    if (confirmed) {
      assert.equal(calls[0][3].replacePrevious, true)
      assert.equal(calls[0][3].expectedTargetFingerprint, 'target')
      assert.deepEqual(Array.from(calls[0][3].targets), ['gfx1100'])
    }
  })
}

test('Windows ROCm repair opens the device selector without installing or bypassing disabled profiles', async () => {
  const actions = []
  const scope = {
    app: { runtimeManagementSupported: true, runtimeInfo: { platform: 'win32' }, installRuntime: () => assert.fail('must review a plan') },
    runtimeBusy: { value: false }, openRocmInstaller: async repair => actions.push(repair),
  }
  await componentFunction('../src/views/SettingsView.vue', 'installBackend', scope)('rocm')
  assert.deepEqual(actions, [true])
})

test('a disabled ROCm plan cannot be dispatched even after preview acknowledgement', async () => {
  const scope = {
    app: { installRuntime: () => assert.fail('disabled profile must remain disabled') },
    rocmPlan: { value: { installationEnabled: false } }, rocmPlanReady: { value: false }, runtimeBusy: { value: false },
  }
  await componentFunction('../src/views/SettingsView.vue', 'confirmRocmInstall', scope)()
})

test('runtime recovery remains available after runtime-info recovery failure without claiming arbitrary query failures require recovery', () => {
  for (const [code, expected] of [['RUNTIME_RECOVERY_FAILED', true], ['RUNTIME_BUSY', false]]) {
    const result = componentComputed('../src/views/SettingsView.vue', 'runtimeRecoveryNeeded', {
      app: { runtimeInfoFailure: { code } },
    })
    assert.equal(Boolean(result), expected)
  }
})

test('rollback confirmation identifies current and previous runtime versions before dispatch', async () => {
  const actions = []
  const scope = {
    app: { runtimeInfo: { previousRuntime: { torchVersion: '2.9.1' } }, runtimeInstallPlan: async () => ({}), rollbackRuntime: async () => actions.push('rollback') },
    runtimeBusy: { value: false }, runtimeMaintenancePreparing: { value: false },
    runtimeEnvironmentForBackend: () => ({ torchVersion: '2.12.0' }),
    confirmRuntimeAction: async (_title, content) => { assert.match(content, /2\.9\.1/); assert.match(content, /2\.12\.0/); return true },
    t: (key, values) => `${key}:${JSON.stringify(values || {})}`, runtimeErrorSummary,
    message: { error: value => assert.fail(value) },
  }
  await componentFunction('../src/views/SettingsView.vue', 'restoreRuntimeEnvironment', scope)()
  assert.deepEqual(actions, ['rollback'])
  assert.equal(scope.runtimeMaintenancePreparing.value, false)
})


for (const includeFp16 of [false, true]) {
  test(`Settings GPU diagnostic sends includeFp16=${includeFp16} and keeps the selected precision visible`, async () => {
    const calls = []
    const scope = {
      app: { startRuntimeGpuProbe: async payload => calls.push(payload) },
      runtimeBusy: { value: false }, runtimeGpuDialogVisible: { value: false }, runtimeGpuIncludeFp16: { value: false },
      runtimeErrorSummary, t: key => key, message: { error: value => assert.fail(value) },
    }
    const probe = componentFunction('../src/views/SettingsView.vue', 'probeRuntimeGpu', scope)
    if (includeFp16) await probe(true)
    else await probe()
    assert.equal(calls[0].includeFp16, includeFp16)
    assert.equal(scope.runtimeGpuIncludeFp16.value, includeFp16)
    assert.equal(scope.runtimeGpuDialogVisible.value, true)
  })
}


for (const locale of ['zh-CN', 'en']) {
  test(`${locale} names known GPU operators and preserves unknown operator identifiers`, () => {
    const messages = JSON.parse(readFileSync(new URL(`../src/i18n/${locale}.json`, import.meta.url), 'utf8')).settings
    const label = componentFunction('../src/views/SettingsView.vue', 'runtimeGpuCheckLabel', {
      t: key => messages[key.replace('settings.', '')],
    })
    for (const id of ['allocation', 'stftIstft', 'conv', 'batchNorm', 'instanceNorm', 'sdpa', 'rnn']) {
      assert.equal(typeof label(id), 'string')
      assert.ok(label(id).length > 0)
      assert.notEqual(label(id), id)
    }
    assert.equal(label('newOperator'), 'newOperator')
    assert.match(label('conv'), locale === 'zh-CN' ? /基础/ : /Basic/)
    assert.match(label('rnn'), locale === 'zh-CN' ? /基础路径/ : /basic path/)
  })

  test(`${locale} explains ROCm inference failures without promising CPU fallback`, () => {
    const messages = JSON.parse(readFileSync(new URL(`../src/i18n/${locale}.json`, import.meta.url), 'utf8')).settings
    const translate = key => messages[key.replace('settings.', '')]
    for (const code of ['ROCM_CONTEXT_LOST', 'ROCM_OUT_OF_MEMORY', 'ROCM_TARGET_MISSING', 'ROCM_RUNTIME_INCOMPATIBLE', 'ROCM_NATIVE_CHECK_FAILED']) {
      const summary = runtimeErrorSummary({ code, message: 'raw diagnostic' }, translate)
      assert.equal(typeof summary, 'string')
      assert.notEqual(summary, messages.runtimeCoreOperationFailed)
      assert.doesNotMatch(summary, /CPU|fallback|raw diagnostic/i)
    }
    const context = runtimeErrorSummary({ code: 'ROCM_CONTEXT_LOST' }, translate)
    assert.match(context, locale === 'zh-CN' ? /重新.*任务/ : /Start a new task/)
    const memory = runtimeErrorSummary({ code: 'ROCM_OUT_OF_MEMORY' }, translate)
    assert.match(memory, locale === 'zh-CN' ? /分段长度.*并发/ : /segment size.*concurrent/)
  })
}
