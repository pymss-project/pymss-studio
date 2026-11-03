import assert from 'node:assert/strict'
import test from 'node:test'
import { buildUpdateDiagnosticSnapshot, type UpdateDiagnosticInput } from '../src/utils/updateDiagnostics.ts'

const idle: UpdateDiagnosticInput = {
  status: 'idle', currentVersion: '', latestVersion: '', lastCheckedAt: '', error: '',
  downloadDownloadedBytes: 0, downloadTotalBytes: 0, downloadSpeedBytesPerSecond: 0, downloadProgressPercent: null,
  restartRequired: false, installFailed: false, integrity: null, integrityError: '', integrityChecking: false, repairing: false, downloadLogs: [],
}

test('unobserved update state stays unknown rather than reporting success', () => {
  const snapshot = buildUpdateDiagnosticSnapshot(idle)
  assert.equal(snapshot.lastCheckedAt, '')
  assert.equal(snapshot.integrity, null)
  assert.equal(snapshot.selectedSource, null)
  assert.equal(snapshot.downloadProgressPercent, null)
})

test('download failure, integrity failure and restart state are retained independently', () => {
  const snapshot = buildUpdateDiagnosticSnapshot({ ...idle, status: 'failed', error: 'Checksum mismatch',
    restartRequired: true, installFailed: true, integrity: { supported: true, valid: false, repairSupported: true, detail: 'Resource changed' } })
  assert.equal(snapshot.error, 'Checksum mismatch')
  assert.equal(snapshot.restartRequired, true)
  assert.equal(snapshot.installFailed, true)
  assert.equal(snapshot.integrity?.valid, false)
})

test('ongoing integrity checks and repair are retained alongside the last result', () => {
  const snapshot = buildUpdateDiagnosticSnapshot({ ...idle, integrityChecking: true, repairing: true,
    integrity: { supported: true, valid: true, repairSupported: true } })
  assert.equal(snapshot.integrityChecking, true)
  assert.equal(snapshot.repairing, true)
})

test('only the latest twenty update log entries are copied', () => {
  const logs = Array.from({ length: 30 }, (_, i) => ({ at: String(i), message: `Message ${i}` }))
  const snapshot = buildUpdateDiagnosticSnapshot({ ...idle, downloadLogs: logs })
  assert.equal(snapshot.downloadLogs.length, 20)
  assert.equal(snapshot.downloadLogs[0].at, '10')
  logs[29].message = 'Changed later'
  assert.equal(snapshot.downloadLogs[19].message, 'Message 29')
})

test('arbitrary store fields are not included in the report snapshot', () => {
  const input = { ...idle, credential: 'private', customEndpoint: 'https://private', releaseNotes: 'arbitrary content' }
  const snapshot = JSON.stringify(buildUpdateDiagnosticSnapshot(input))
  assert.ok(!snapshot.includes('private'))
  assert.ok(!snapshot.includes('arbitrary content'))
})
