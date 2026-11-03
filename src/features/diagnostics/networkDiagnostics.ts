import { ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { redactDiagnosticText } from '@/utils/logger'

export type NetworkCheck = {
  id: 'models' | 'pypi' | 'plugins' | 'updates'
  status: 'not_checked' | 'checking' | 'ok' | 'failed' | 'timeout' | 'unsupported'
  kind?: string; source?: string; host?: string; httpStatus?: number; elapsedMs?: number
  error?: string; errorCode?: string; gitAvailable?: boolean; catalogCount?: number; outcome?: string; sourcePreference?: string
}
export type NetworkConfiguration = {
  modelSource: string; pluginSource: 'auto' | 'github' | 'gitcode'; proxyMode: string
  updateSource: 'auto' | 'github' | 'modelscope'; channel: 'stable' | 'prerelease'; locale: string
  endpointOverride: string
}
export type NetworkDiagnosticSnapshot = {
  checkedAt: string
  observedProxyMode?: string
  configuration: Omit<NetworkConfiguration, 'endpointOverride'> & { customUpdateEndpoint: boolean }
  checks: NetworkCheck[]
}

export function useNetworkDiagnostics() {
  const snapshot = ref<NetworkDiagnosticSnapshot | null>(null)
  const busy = ref(false)
  let generation = 0
  function invalidate() { generation++; snapshot.value = null }
  async function check(configuration: NetworkConfiguration, prepare: () => Promise<void>) {
    if (busy.value) return
    const request = ++generation
    busy.value = true
    const { endpointOverride, ...safeConfiguration } = configuration
    snapshot.value = { checkedAt: new Date().toISOString(), configuration: { ...safeConfiguration, customUpdateEndpoint: Boolean(endpointOverride) },
      checks: (['models', 'pypi', 'plugins', 'updates'] as const).map(id => ({ id, status: 'checking' })) }
    const update = (rows: NetworkCheck[]) => {
      if (request !== generation || !snapshot.value) return
      snapshot.value.checks = snapshot.value.checks.map(row => rows.find(item => item.id === row.id) || row)
    }
    const failure = (id: NetworkCheck['id'], error: unknown, elapsedMs: number): NetworkCheck => {
      const detail = redactDiagnosticText(error).slice(0, 600)
      return { id, status: id === 'updates' && detail.includes('UPDATE_DISTRIBUTION_UNSUPPORTED') ? 'unsupported' : 'failed', elapsedMs, error: detail }
    }
    try {
      // Flush the existing in-memory proxy sync before creating any workers/clients.
      await prepare()
      if (request !== generation) return
      await Promise.all([
        (async () => {
          const start = performance.now()
          try {
            const result = await invoke<{ checks: NetworkCheck[]; proxyMode?: string }>('network_diagnostics', { payload: { modelSource: configuration.modelSource, mirror: 'auto', locale: configuration.locale } })
            if (request === generation && snapshot.value) snapshot.value.observedProxyMode = result.proxyMode
            update(result.checks.filter(row => row.id === 'models' || row.id === 'pypi').map(row => ({
              id: row.id, status: row.status, kind: row.kind, source: row.source, host: row.host,
              httpStatus: row.httpStatus, elapsedMs: row.elapsedMs, errorCode: row.errorCode,
              error: row.error ? redactDiagnosticText(row.error).slice(0, 600) : undefined,
            })))
          } catch (error) { update(['models', 'pypi'].map(id => failure(id as NetworkCheck['id'], error, performance.now() - start))) }
        })(),
        (async () => {
          const start = performance.now()
          try {
            const result = await invoke<{ catalog?: unknown[]; catalogError?: string | null; catalogSource?: { supported: boolean; provider?: string; gitAvailable?: boolean; preference?: string } }>('plugin_marketplace', { payload: { catalogOnly: true, includeCatalog: true } })
            update([{ id: 'plugins', kind: 'catalog', status: result.catalogSource?.supported === false || !Array.isArray(result.catalog) ? 'unsupported' : result.catalogError ? 'failed' : 'ok',
              source: result.catalogSource?.provider, gitAvailable: result.catalogSource?.gitAvailable,
              sourcePreference: result.catalogSource?.preference,
              catalogCount: result.catalog?.length, elapsedMs: performance.now() - start,
              error: result.catalogError ? redactDiagnosticText(result.catalogError).slice(0, 600) : undefined }])
          } catch (error) { update([failure('plugins', error, performance.now() - start)]) }
        })(),
        (async () => {
          const start = performance.now()
          try {
            const result = await invoke<{ source?: string } | null>('check_managed_update', { channel: configuration.channel, source: configuration.updateSource, endpointOverride: endpointOverride || null })
            update([{ id: 'updates', kind: 'update_metadata', status: 'ok', source: result?.source,
              elapsedMs: performance.now() - start, outcome: result ? 'available' : 'no_update' }])
          } catch (error) { update([failure('updates', error, performance.now() - start)]) }
        })(),
      ])
    } catch (error) { update((['models', 'pypi', 'plugins', 'updates'] as const).map(id => failure(id, error, 0))) }
    finally {
      if (request === generation && snapshot.value) snapshot.value.checkedAt = new Date().toISOString()
      busy.value = false
    }
  }
  return { snapshot, busy, check, invalidate }
}
