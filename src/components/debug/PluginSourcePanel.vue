<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { open } from '@tauri-apps/plugin-dialog'
import { useDialog, useMessage, type DialogReactive } from 'naive-ui'
import { useI18n } from 'vue-i18n'
import { FolderOpenOutline, RefreshOutline } from '@vicons/ionicons5'
import { usePluginMarketplace, type CatalogPlugin } from '@/features/plugin-marketplace/composable'

type Source = { mode: 'local' | 'repository'; localPath?: string | null; repositoryUrl?: string | null; ref?: string | null; registryRef?: string | null }
type SourceInfo = {
  defaults: Source; effective: Source & { origin?: string }; override?: Source | null
  available: boolean | null; error: string | null; catalogCount: number | null
  registryUrl?: string | null; registryPath?: string | null; configPath: string; settingsPath?: string
}
const { t, locale } = useI18n()
const message = useMessage()
const dialog = useDialog()
const market = usePluginMarketplace()
const info = ref<SourceInfo | null>(null)
const draft = reactive({ mode: 'repository' as Source['mode'], localPath: '', repositoryUrl: '', ref: 'main', registryRef: 'main' })
const loading = ref(false), saving = ref(false), acquiring = ref(false), confirming = ref(false)
const error = ref(''), baseline = ref(''), catalogSource = ref('')
const catalogRuntime = ref('')
const catalog = ref<CatalogPlugin[]>([])
const selection = ref<string[]>([])
const completed = ref<string[]>([])
let sequence = 0, disposed = false, stopRequested = false
let acquisitionDialog: DialogReactive | undefined
const busy = computed(() => loading.value || saving.value || acquiring.value || confirming.value || market.busy.value || market.loading.value || market.catalogLoading.value)
const sourceValue = () => draft.mode === 'local'
  ? { mode: 'local' as const, localPath: draft.localPath.trim() }
  : { mode: 'repository' as const, repositoryUrl: draft.repositoryUrl.trim(), ref: draft.ref.trim(), registryRef: draft.registryRef.trim() }
const dirty = computed(() => JSON.stringify(sourceValue()) !== baseline.value)
const valid = computed(() => draft.mode === 'local' ? Boolean(draft.localPath.trim()) : Boolean(draft.repositoryUrl.trim() && draft.ref.trim() && draft.registryRef.trim()))
const installed = computed(() => new Map((market.snapshot.value?.installed || []).filter(item => item.managed).map(item => [item.name, item])))
const modes = computed(() => [{ label: t('debugPlugins.localMode'), value: 'local' }, { label: t('debugPlugins.repositoryMode'), value: 'repository' }])
const signature = (value: SourceInfo) => JSON.stringify({ ...value.effective, origin: undefined, registryUrl: value.registryUrl })

function adopt(value: SourceInfo) {
  info.value = value
  const selected = value.override || value.effective
  draft.mode = selected.mode || value.defaults.mode || 'repository'
  draft.localPath = selected.localPath || value.defaults.localPath || ''
  draft.repositoryUrl = selected.repositoryUrl || value.defaults.repositoryUrl || ''
  draft.ref = selected.ref || value.defaults.ref || 'main'
  draft.registryRef = selected.registryRef || value.defaults.registryRef || 'main'
  baseline.value = JSON.stringify(sourceValue())
  catalog.value = []; selection.value = []; catalogSource.value = ''; catalogRuntime.value = ''; completed.value = []
}

async function loadSource() {
  if (loading.value || saving.value || acquiring.value || confirming.value) return
  const current = ++sequence
  loading.value = true; error.value = ''
  try {
    const value = await invoke<SourceInfo>('debug_plugin_source')
    if (!disposed && current === sequence) adopt(value)
  } catch (failure) { if (!disposed && current === sequence) error.value = String(failure) }
  finally { if (!disposed && current === sequence) loading.value = false }
}

async function readCatalog() {
  if (busy.value || dirty.value || !info.value || info.value.error) return
  const expected = signature(info.value), current = ++sequence
  error.value = ''; catalog.value = []; selection.value = []; completed.value = []
  await market.refresh()
  if (disposed || current !== sequence) return
  if (market.error.value || market.snapshot.value?.catalogError) {
    error.value = market.error.value || market.snapshot.value?.catalogError || ''
    return
  }
  catalog.value = [...(market.snapshot.value?.catalog || [])]
  const defaults = new Set(market.snapshot.value?.preinstalled?.map(item => item.name) || [])
  selection.value = catalog.value.filter(item => defaults.has(item.name)).map(item => item.name)
  catalogSource.value = expected
  catalogRuntime.value = market.snapshot.value?.runtime.identity || ''
}

async function saveSource(reset = false) {
  if (busy.value || (!reset && !valid.value)) return
  const current = ++sequence
  saving.value = true; error.value = ''
  try {
    const value = await invoke<SourceInfo>('debug_save_plugin_source', { source: reset ? null : sourceValue() })
    if (!disposed && current === sequence) { adopt(value); message.success(t('debugPlugins.saved')) }
  } catch (failure) { if (!disposed && current === sequence) error.value = String(failure) }
  finally { if (!disposed && current === sequence) saving.value = false }
  if (!disposed && !error.value) await readCatalog()
}

async function chooseDirectory() {
  if (busy.value) return
  const current = sequence
  try {
    const selected = await open({ directory: true, multiple: false, title: t('debugPlugins.chooseDirectory') })
    if (!disposed && current === sequence && typeof selected === 'string') draft.localPath = selected
  } catch (failure) { if (!disposed) error.value = String(failure) }
}

async function acquire(names: string[]) {
  acquiring.value = true; stopRequested = false; error.value = ''; completed.value = []
  const expected = catalogSource.value
  try {
    for (const name of names) {
      if (disposed || stopRequested) break
      const current = await invoke<SourceInfo>('debug_plugin_source')
      if (disposed || stopRequested) break
      if (signature(current) !== expected) throw new Error(t('debugPlugins.sourceChanged'))
      await market.manage(installed.value.has(name) ? 'update' : 'install', { name, locale: locale.value, expectedSource: expected, expectedRuntime: catalogRuntime.value })
      if (!disposed) completed.value.push(name)
    }
    if (!disposed && !stopRequested) message.success(t('plugins.actionComplete'))
  } catch (failure) { if (!disposed) error.value = String(failure) }
  finally { if (!disposed) acquiring.value = false }
}

function confirmAcquisition() {
  if (busy.value || dirty.value || !selection.value.length || !catalogSource.value || !catalogRuntime.value) return
  const names = [...selection.value]
  confirming.value = true
  const dismiss = () => { confirming.value = false }
  acquisitionDialog = dialog.warning({
    title: t('debugPlugins.acquire'), content: t('debugPlugins.confirmAcquire', { names: names.join(', ') }),
    positiveText: t('debugPlugins.start'), negativeText: t('common.cancel'),
    onPositiveClick: () => { dismiss(); if (!disposed) void acquire(names) },
    onNegativeClick: dismiss, onClose: dismiss, onEsc: dismiss, onMaskClick: dismiss,
  })
}

async function cancelAcquisition() {
  stopRequested = true
  try { if (market.busy.value) await market.cancel() }
  catch (failure) { if (!disposed) error.value = String(failure) }
}

onMounted(async () => { await loadSource(); if (!disposed && info.value && !info.value.error) await readCatalog() })
onBeforeUnmount(() => { disposed = true; sequence++; stopRequested = true; acquisitionDialog?.destroy() })
</script>

<template>
  <section class="plugin-source-panel">
    <n-card size="small" :title="t('debugPlugins.title')">
    <template #header-extra><n-button size="small" :disabled="busy" :loading="loading" @click="loadSource"><template #icon><n-icon :component="RefreshOutline" /></template>{{ t('common.refresh') }}</n-button></template>
      <p class="source-hint">{{ t('debugPlugins.description') }}</p>
      <n-alert v-if="error || info?.error" type="error" :bordered="false">{{ error || info?.error }}</n-alert>
      <n-alert v-if="info?.effective.origin === 'registry-environment'" type="warning" :bordered="false">{{ t('debugPlugins.environmentOverride') }}</n-alert>
      <div class="source-form">
        <div class="source-field"><label>{{ t('debugPlugins.mode') }}</label><n-select v-model:value="draft.mode" :options="modes" :disabled="busy" /></div>
        <div v-if="draft.mode === 'local'" class="source-field source-field--wide">
          <label>{{ t('debugPlugins.localPath') }}</label>
          <div class="source-path"><n-input v-model:value="draft.localPath" :disabled="busy" :placeholder="t('debugPlugins.localPlaceholder')" /><n-button :disabled="busy" @click="chooseDirectory"><template #icon><n-icon :component="FolderOpenOutline" /></template>{{ t('common.browse') }}</n-button></div>
          <small>{{ t('debugPlugins.localHint') }}</small>
        </div>
        <template v-else>
          <div class="source-field source-field--wide"><label>{{ t('debugPlugins.repositoryUrl') }}</label><n-input v-model:value="draft.repositoryUrl" :disabled="busy" placeholder="https://github.com/owner/repository" /></div>
          <div class="source-field"><label>{{ t('debugPlugins.registryRef') }}</label><n-input v-model:value="draft.registryRef" :disabled="busy" /></div>
        </template>
      </div>
      <div class="source-actions">
        <span v-if="dirty" class="source-hint">{{ t('debugPlugins.unsaved') }}</span>
        <n-button :disabled="busy || (!info?.override && !dirty)" @click="saveSource(true)">{{ t('debugPlugins.reset') }}</n-button>
        <n-button type="primary" :disabled="busy || !valid" :loading="saving" @click="saveSource(false)">{{ t('common.save') }}</n-button>
      </div>
      <details v-if="info" class="source-details"><summary>{{ t('debugPlugins.effectiveSource') }}</summary>
        <dl><dt>{{ t('debugPlugins.registry') }}</dt><dd>{{ info.registryPath || info.registryUrl || '—' }}</dd>
          <dt>{{ t('debugPlugins.settingsPath') }}</dt><dd>{{ info.settingsPath || '—' }}</dd>
          <dt>{{ t('debugPlugins.installPath') }}</dt><dd>{{ market.snapshot.value?.pluginsDir || '—' }}</dd></dl>
      </details>
    </n-card>

    <n-card size="small" :title="t('debugPlugins.catalogTitle')">
      <template #header-extra><n-button size="small" :disabled="busy || dirty || !info || Boolean(info.error)" :loading="market.loading.value" @click="readCatalog">{{ t('debugPlugins.readCatalog') }}</n-button></template>
      <p class="source-hint">{{ t('debugPlugins.acquireHint') }}</p>
      <div v-if="catalog.length" class="source-selection">
        <n-checkbox-group v-model:value="selection" :disabled="busy || dirty">
          <div v-for="plugin in catalog" :key="plugin.name" class="source-plugin-row">
            <n-checkbox :value="plugin.name" :label="plugin.name" :disabled="busy || dirty" :aria-disabled="busy || dirty" />
            <span class="source-plugin-description">{{ plugin.description }}</span>
            <n-tag v-if="installed.has(plugin.name)" size="small" :bordered="false">{{ installed.get(plugin.name)?.enabled === false ? t('plugins.disabled') : t('plugins.installed') }}</n-tag>
          </div>
        </n-checkbox-group>
      </div>
      <n-empty v-else :description="t('debugPlugins.noCatalog')" />
      <div class="source-actions">
        <small v-if="completed.length">{{ t('debugPlugins.completed', { count: completed.length }) }}</small>
        <n-button text :disabled="busy || dirty || !catalog.length" @click="selection = selection.length === catalog.length ? [] : catalog.map(item => item.name)">{{ t('debugPlugins.selectAll') }}</n-button>
        <n-button v-if="acquiring" @click="cancelAcquisition">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :disabled="busy || dirty || !selection.length || !catalogSource || !catalogRuntime" :loading="acquiring" @click="confirmAcquisition">{{ t('debugPlugins.acquire') }}<template v-if="selection.length"> ({{ selection.length }})</template></n-button>
      </div>
      <pre v-if="market.logs.value.length" class="source-log console-scrollbar" role="log" aria-live="polite">{{ market.logs.value.join('\n') }}</pre>
    </n-card>
  </section>
</template>

<style scoped>
.plugin-source-panel { display: grid; grid-template-columns: minmax(300px, .9fr) minmax(0, 1.1fr); align-items: start; gap: 16px; max-width: 1400px; }
.source-hint { color: var(--on-surface-muted); font-size: 12px; line-height: 1.65; }
.source-form { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; margin-top: 18px; }
.source-field { display: grid; align-content: start; gap: 7px; min-width: 0; }
.source-field > label { font-size: 12px; font-weight: 600; }
.source-field small { color: var(--on-surface-muted); font-size: 11px; }
.source-field--wide { grid-column: 1 / -1; }
.source-path { display: flex; gap: 8px; }.source-path > .n-input { min-width: 0; }
.source-actions { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 10px; margin-top: 18px; }
.source-actions > small { margin-right: auto; color: var(--on-surface-muted); }
.source-details { margin-top: 18px; color: var(--on-surface-muted); font-size: 12px; }
.source-details summary { cursor: pointer; }
.source-details dl { display: grid; grid-template-columns: 100px minmax(0, 1fr); gap: 10px; }
.source-details dd { margin: 0; overflow-wrap: anywhere; font-family: var(--font-mono); font-size: 11px; }
.source-selection { max-height: 420px; overflow: auto; scrollbar-width: thin; }
.source-plugin-row { display: flex; align-items: center; gap: 12px; padding: 12px 4px; border-bottom: 1px solid var(--outline); }
.source-plugin-row > .n-checkbox { min-width: 125px; }
.source-plugin-description { flex: 1; color: var(--on-surface-muted); font-size: 12px; }
.source-log { max-height: 260px; overflow: auto; padding: 12px; border-radius: 8px; background: var(--surface-2); color: var(--on-surface-muted); font: 11px/1.6 var(--font-mono); white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 700px) { .source-form { grid-template-columns: minmax(0, 1fr); }.source-plugin-description { display: none; }.source-plugin-row > .n-tag { margin-left: auto; } }
@media (max-width: 1100px) { .plugin-source-panel { grid-template-columns: minmax(0, 1fr); } }
</style>
