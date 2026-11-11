import assert from 'node:assert/strict'
import test from 'node:test'
import { rocmDeviceTargets, rocmDefaultDeviceIds, rocmStableDeviceIds, rocmProfileSelectable, rocmPreferredProfileId, rocmInstallAction, runtimeInstallPlanMatches, runtimeInstallOptionsFromPlan, runtimeGpuStatus } from '../src/utils/runtime.ts'

const profile = { id: 'multi', revision: 2, releaseState: 'preview', installationEnabled: true, availableTargets: ['all', 'gfx1030', 'gfx1100'], validatedTargets: [] }
const device = (id, gfx, extra = {}) => ({ id, vendor: 'amd', name: 'AMD adapter', gfx, architectureSource: 'native', architectureConfidence: 'confirmed', ...extra })

test('live GPU failure overrides a prior report and a minimum kernel pass does not imply full operator validation', () => {
  const report = { status: 'completed', deviceCapabilities: [{ basicAvailable: true, modes: [] }] }
  assert.equal(runtimeGpuStatus(report), 'available')
  assert.equal(runtimeGpuStatus(report, { status: 'failed' }), 'unavailable')
  assert.equal(runtimeGpuStatus(undefined, { status: 'failed' }), 'unavailable')
  assert.equal(runtimeGpuStatus(undefined, { status: 'passed' }), 'unverified')
})

test('only confirmed device architectures automatically select deduplicated targets', () => {
  const devices = [device('one', 'gfx1100'), device('two', 'gfx1100'), device('hint', 'gfx1030', { architectureConfidence: 'hint' }), device('unknown', null)]
  assert.deepEqual(rocmDeviceTargets(devices, ['one', 'two', 'hint', 'unknown']), ['gfx1100'])
  assert.deepEqual(rocmDeviceTargets(devices, ['hint', 'unknown']), [])
  assert.deepEqual(rocmDeviceTargets(devices, ['one'], ['gfx1030']), ['gfx1030', 'gfx1100'])
  assert.deepEqual(rocmDeviceTargets(devices, ['one'], ['all']), ['all'])
  assert.deepEqual(rocmDeviceTargets([], [], ['device-all', 'bad', 'gfx1030']), ['gfx1030'])
})

test('stable device IDs survive enumeration reordering and unknown multi-card systems have no guessed selection', () => {
  const devices = [device('igpu', 'gfx1030', { isDiscrete: false }), device('dgpu', 'gfx1100', { isDiscrete: true })]
  assert.deepEqual(rocmDefaultDeviceIds(devices, ['igpu']), ['igpu'])
  assert.deepEqual(rocmDefaultDeviceIds(devices.toReversed(), ['igpu']), ['igpu'])
  assert.deepEqual(rocmDefaultDeviceIds(devices, ['removed']), ['dgpu'])
  assert.deepEqual(rocmDefaultDeviceIds([device('a', null), device('b', null)], []), [])
  assert.deepEqual(rocmDefaultDeviceIds([{ ...device('nvidia', 'gfx1100'), vendor: 'nvidia' }], []), [])
})

test('disabled and revoked releases remain blocked independently of preview acknowledgement', () => {
  for (const releaseState of ['disabled', 'revoked']) assert.equal(rocmProfileSelectable({ ...profile, releaseState }), false)
  for (const releaseState of ['stable', 'legacy', 'preview']) assert.equal(rocmProfileSelectable({ ...profile, releaseState }), true)
  assert.equal(rocmProfileSelectable({ ...profile, installationEnabled: false }), false)
})

test('new installs select the published ROCm default while existing legacy installs retain their profile', () => {
  const legacy = { ...profile, id: 'legacy', releaseState: 'legacy', availableTargets: [] }
  const catalog = [legacy, profile]
  assert.equal(rocmPreferredProfileId(catalog, 'multi', undefined, ['gfx1100']), 'multi')
  assert.equal(rocmPreferredProfileId(catalog, 'multi', undefined, []), 'multi')
  assert.equal(rocmPreferredProfileId(catalog, 'multi', 'legacy', ['gfx1100']), 'legacy')
  assert.equal(rocmPreferredProfileId(catalog, 'multi', undefined, ['gfx9999']), '')
  assert.equal(rocmPreferredProfileId([{ ...profile, installationEnabled: false }], 'multi', 'multi', []), '')
})

test('preinstalled coverage is used directly while missing kernels and ABI changes use distinct actions', () => {
  const env = { profileId: 'multi', profileRevision: 2, installedTargets: ['gfx1100'], health: 'ready' }
  assert.equal(rocmInstallAction(undefined, profile, ['gfx1100']), 'install')
  assert.equal(rocmInstallAction(env, profile, ['gfx1100']), 'useInstalled')
  assert.equal(rocmInstallAction(env, profile, ['gfx1030', 'gfx1100']), 'addTargets')
  assert.equal(rocmInstallAction({ ...env, installedTargets: ['gfx1100', 'gfx1030'] }, profile, ['all']), 'useInstalled')
  assert.equal(rocmInstallAction(env, { ...profile, revision: 3 }, ['gfx1100']), 'upgrade')
  assert.equal(rocmInstallAction(env, { ...profile, id: 'next' }, ['gfx1100']), 'upgrade')
  assert.equal(rocmInstallAction({ ...env, health: 'broken' }, profile, ['gfx1100']), 'install')
})

test('a pre-profile installation can repair its recognized build while a different build requires an upgrade', () => {
  const legacy = { torchVersion: '2.9.1+rocm7.2.1', health: 'broken' }
  const selected = { ...profile, id: 'legacy', torchVersion: legacy.torchVersion, availableTargets: [] }
  assert.equal(rocmInstallAction(legacy, selected, []), 'install')
  assert.equal(rocmInstallAction({ ...legacy, health: 'ready' }, selected, []), 'useInstalled')
  assert.equal(rocmInstallAction(legacy, { ...profile, torchVersion: '2.13.0+rocm10.0.0' }, ['gfx1100']), 'upgrade')
  assert.equal(rocmInstallAction({ health: 'broken' }, selected, []), 'upgrade')
  assert.equal(rocmPreferredProfileId([profile, selected], 'multi', undefined, [], legacy.torchVersion), 'legacy')
  assert.equal(rocmPreferredProfileId([profile, selected, { ...selected, id: 'ambiguous' }], 'multi', undefined, [], legacy.torchVersion), 'multi')
})

test('reviewed plans bind the requested profile/action/target set and forward every concurrency guard', () => {
  const request = { profileId: 'multi', targets: ['gfx1100', 'gfx1030'], action: 'addTargets' }
  const plan = { backend: 'rocm', ...request, requestedTargets: ['gfx1030', 'gfx1100'], resolvedTargets: ['gfx1030', 'gfx1100'], installationEnabled: true,
    expectedProfileId: 'multi', expectedProfileRevision: 2, expectedBaseFingerprint: 'base', expectedTargetFingerprint: 'targets' }
  assert.equal(runtimeInstallPlanMatches(plan, request), true)
  for (const change of [{ profileId: 'different' }, { action: 'upgrade' }, { targets: ['all'] }]) assert.equal(runtimeInstallPlanMatches(plan, { ...request, ...change }), false)
  const options = runtimeInstallOptionsFromPlan(plan)
  assert.equal(options.expectedProfileId, 'multi')
  assert.equal(options.expectedProfileRevision, 2)
  assert.equal(options.expectedBaseFingerprint, 'base')
  assert.equal(options.expectedTargetFingerprint, 'targets')
  assert.deepEqual(options.targets, ['gfx1030', 'gfx1100'])
  assert.equal(runtimeInstallOptionsFromPlan({ ...plan, action: 'prepareSeed' }).action, 'prepareSeed')
})

test('session-only device identifiers are never reused as a saved hardware selection', () => {
  const devices = [device('session:first', null), device('pnp:stable', null)]
  assert.deepEqual(rocmDefaultDeviceIds(devices, ['session:first']), [])
  assert.deepEqual(rocmDefaultDeviceIds(devices, ['pnp:stable']), ['pnp:stable'])
})


test('installation binds only stable physical hardware IDs and a reviewed plan cannot change the selected cards', () => {
  const ids = ['session:first', 'pnp:hash', 'hip-pci:0000:03:00.0', 'hip-pci:0000:03:00.0', 'cuda:0', 'pnp:']
  assert.deepEqual(rocmStableDeviceIds(ids), ['hip-pci:0000:03:00.0', 'pnp:hash'])
  const request = { profileId: 'multi', action: 'addTargets', targets: ['gfx1100'], selectedDeviceIds: ['pnp:hash', 'hip-pci:0000:03:00.0'] }
  const plan = { backend: 'rocm', installationEnabled: true, ...request, selectedDeviceIds: ['hip-pci:0000:03:00.0', 'pnp:hash'] }
  assert.equal(runtimeInstallPlanMatches(plan, request), true)
  assert.equal(runtimeInstallPlanMatches({ ...plan, selectedDeviceIds: ['hip-pci:0000:04:00.0'] }, request), false)
  assert.equal(runtimeInstallPlanMatches({ ...plan, selectedDeviceIds: undefined }, request), false)
  assert.deepEqual(runtimeInstallOptionsFromPlan(plan).selectedDeviceIds, ['hip-pci:0000:03:00.0', 'pnp:hash'])
  assert.deepEqual(runtimeInstallOptionsFromPlan({ ...plan, selectedDeviceIds: ids }).selectedDeviceIds, ['hip-pci:0000:03:00.0', 'pnp:hash'])
})
