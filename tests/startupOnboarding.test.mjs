import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'
import { computed, effectScope, nextTick, reactive, ref, watch } from 'vue'
import { detectRuntimePlatform, preferredInstalledRuntimeBackend, recommendedRuntimeBackend, runtimeEnvironmentForBackend, supportsRuntimeManagement } from '../src/utils/runtime.ts'

const source = readFileSync(new URL('../src/components/StartupOnboarding.vue', import.meta.url), 'utf8')
  .match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const parsed = ts.createSourceFile('StartupOnboarding.ts', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const variables = new Set([
  'step', 'checkingModelDir', 'runtimeChecking', 'runtimeActionError',
  'finishing', 'runtimePlatform', 'selectedBackend', 'runtimeBackendChoices',
  'runtimeRecommendedBackend', 'preferredRuntimeBackend', 'backendTouched', 'selectedBackendInstalled',
  'selectedBackendReady', 'selectedRuntime', 'runtimeBusy', 'runtimeError', 'runtimeStatusKey',
  'runtimeRecommendationKey', 'primaryActionLabel', 'stepList', 'totalSteps', 'stepMeta', 'currentStep', 'canGoNext',
])
const functions = new Set([
  'backendInstalled', 'nextStep', 'previousStep', 'centerOrigin', 'refreshRuntime',
  'finishOnboarding', 'primaryAction',
])
const statements = parsed.statements.filter(node => {
  if (ts.isVariableStatement(node)) return node.declarationList.declarations.some(item => variables.has(item.name.getText(parsed)))
  if (ts.isFunctionDeclaration(node)) return functions.has(node.name?.text)
  return ts.isExpressionStatement(node) && ts.isCallExpression(node.expression)
    && node.expression.expression.getText(parsed) === 'watch'
})
const code = ts.transpileModule(statements.map(node => node.getText(parsed)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText

function environment(t, options = {}) {
  const actions = []
  const platform = options.platform || 'win32'
  const app = reactive({
    runtimeInfo: { platform, machine: options.machine || 'arm64', ready: Boolean(options.ready),
      torchBackend: options.backend || 'cpu', packages: { mlx: platform === 'darwin' },
      installedEnvironments: options.installed || [],
    },
    runtimeInfoError: '', runtimeInstallStatus: 'idle', runtimeInstallBackend: null, runtimeInstallMessage: '',
    runtimeManagementSupported: supportsRuntimeManagement({ platform }),
    runtimeReadyForBackend(backend) {
      return this.runtimeInfo.ready && (backend === 'mlx' ? this.runtimeInfo.packages.mlx : this.runtimeInfo.torchBackend === backend)
    },
    async checkRuntimeInfo() { actions.push('check'); await options.onCheck?.(app) },
    async installRuntime() { actions.push('install') },
    async activateRuntime() { actions.push('activate') },
    async cancelRuntimeInstall() { actions.push('cancel') },
  })
  const scope = effectScope()
  const inputs = {
    app, modelDirMigrationState: ref({ status: 'idle' }), isModelDirMigrating: ref(false),
    settings: { markStartupOnboardingCompleted: async () => actions.push('complete') },
    router: { push: async route => actions.push(`route:${route.name}${route.query ? ':' + route.query.section + ':' + route.query.backend : ''}`) },
    message: { error: text => actions.push(`error:${text}`) },
    emit: () => actions.push('emitted'),
    computed, ref, watch, Error, detectRuntimePlatform, preferredInstalledRuntimeBackend, recommendedRuntimeBackend, runtimeEnvironmentForBackend,
    runtimeBackendName: backend => backend, runtimeSizeHint: () => '', isTauriRuntime: () => true,
    t: key => key, runRippleViewTransition: async action => action(), window: { innerWidth: 1280, innerHeight: 800 },
    MusicalNotesOutline: 'welcome', ColorPaletteOutline: 'appearance', FolderOpenOutline: 'directory', HardwareChipOutline: 'runtime',
  }
  const exposed = scope.run(() => vm.runInNewContext(`${code}\n({ ${[...variables, ...functions].join(', ')} })`, inputs))
  t.after(() => scope.stop())
  return { ...inputs, ...exposed, actions }
}

async function settle() {
  await nextTick()
  await new Promise(resolve => setImmediate(resolve))
  await nextTick()
}

test('four setup steps retain appearance and directory confirmation', t => {
  const env = environment(t)
  assert.deepEqual(Array.from(env.stepList.value, step => step.key), ['welcome', 'appearance', 'dataDir', 'runtime'])
  env.step.value = 2
  env.modelDirMigrationState.value.status = 'confirm'
  assert.equal(env.canGoNext.value, false)
  env.modelDirMigrationState.value.status = 'running'
  env.isModelDirMigrating.value = true
  assert.equal(env.canGoNext.value, false)
})

for (const machine of ['arm64', 'aarch64', 'x86_64']) {
  test(`macOS/${machine} only selects MLX and never installs or activates an environment`, async t => {
    const env = environment(t, { platform: 'darwin', machine })
    assert.equal(env.selectedBackend.value, 'mlx')
    assert.deepEqual(Array.from(env.runtimeBackendChoices.value, item => item.value), ['mlx'])
    await env.finishOnboarding()
    assert.deepEqual(env.actions, [])
    env.step.value = 3
    await env.primaryAction({})
    assert.deepEqual(env.actions, ['route:settings:runtime:mlx', 'complete', 'emitted'])
  })
}

for (const backend of ['cpu', 'cuda', 'rocm']) {
  for (const installed of [false, true]) {
    test(`${backend} ${installed ? 'activation' : 'installation'} is handed to runtime management`, async t => {
      const env = environment(t, { installed: installed ? [{ backend, pythonPath: '/runtime/python' }] : [] })
      env.selectedBackend.value = backend
      env.backendTouched.value = true
      env.step.value = 3
      assert.equal(env.primaryActionLabel.value, 'onboarding.manageRuntime')
      await env.primaryAction({})
      assert.deepEqual(env.actions, [`route:settings:runtime:${backend}`, 'complete', 'emitted'])
    })
  }
}

test('an ongoing installation is preserved while leaving setup for management', async t => {
  const env = environment(t, { ready: true })
  env.app.runtimeInstallBackend = 'cpu'
  env.app.runtimeInstallStatus = 'installing'
  env.step.value = 3
  await settle()
  await env.refreshRuntime()
  assert.equal(env.selectedBackendReady.value, false)
  assert.equal(env.runtimeBusy.value, false)
  await env.primaryAction({})
  assert.deepEqual(env.actions, ['route:settings:runtime:cpu', 'complete', 'emitted'])
  assert.equal(env.app.runtimeInstallStatus, 'installing')
})

test('Linux onboarding hands CPU installation to runtime management', async t => {
  const env = environment(t, { platform: 'linux', machine: 'x86_64' })
  env.selectedBackend.value = 'cpu'
  env.step.value = 3
  assert.equal(env.app.runtimeManagementSupported, true)
  await env.primaryAction({})
  assert.deepEqual(env.actions, ['route:settings:runtime:cpu', 'complete', 'emitted'])
})

test('a runtime detection failure can continue to management without changing the environment', async t => {
  const env = environment(t, { onCheck: () => { throw new Error('Runtime unavailable') } })
  env.step.value = 3
  await env.refreshRuntime()
  assert.equal(env.runtimeError.value, 'Runtime unavailable')
  await env.primaryAction({})
  assert.deepEqual(env.actions, ['check', 'route:settings:runtime:cpu', 'complete', 'emitted'])
})

for (const platform of ['win32', 'darwin']) {
  test(`a ready ${platform} runtime completes setup without checking the model inventory`, async t => {
    const env = environment(t, { platform, ready: true })
    env.step.value = 3
    await settle()
    assert.equal(env.primaryActionLabel.value, 'onboarding.startUsing')
    await Promise.all([env.finishOnboarding(), env.finishOnboarding()])
    assert.deepEqual(env.actions, ['route:separate', 'complete', 'emitted'])
  })
}

test('skipping setup during a background installation allows browsing without cancelling it', async t => {
  const env = environment(t)
  env.app.runtimeInstallStatus = 'installing'
  await env.finishOnboarding(undefined, true)
  assert.deepEqual(env.actions, ['route:separate', 'complete', 'emitted'])
  assert.equal(env.app.runtimeInstallStatus, 'installing')
})
