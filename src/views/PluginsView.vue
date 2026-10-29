<script setup lang="ts">
import { computed, h, onMounted, ref, watch, type Component } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { open } from '@tauri-apps/plugin-dialog'
import { NIcon, useDialog, useMessage, type DropdownOption, type DropdownDividerOption } from 'naive-ui'
import { ExtensionPuzzleOutline, RefreshOutline, SearchOutline, FolderOpenOutline, EllipsisHorizontalOutline, TerminalOutline, ArrowUpCircleOutline, BuildOutline, TrashOutline, CheckmarkCircleOutline, PowerOutline } from '@vicons/ionicons5'
import { usePluginMarketplace, type InstalledPlugin, type PluginAction, type PluginCompatibility } from '@/features/plugin-marketplace/composable'
import { useAppStore } from '@/stores/app'
import PluginLogDialog from '@/features/plugin-marketplace/PluginLogDialog.vue'
import { normalizePluginCategory, pluginCategories, pluginLocale, pluginSearchText } from '@/features/plugin-marketplace/presentation'
import { pluginToolCategories } from '@/features/plugin-marketplace/toolEntries'

const { t, locale } = useI18n()
const router = useRouter()
const dialog = useDialog()
const message = useMessage()
const app = useAppStore()
const { snapshot, updates, loading, catalogLoading, error, busy, exclusiveBusy, logs, logRecords, activity, activities, openMarketplace, refreshInventory, manage, cancel } = usePluginMarketplace()
const tab = ref<'browse' | 'installed'>('browse')
const search = ref('')
const category = ref('all')
const categoryLabels = { convert: 'tools.categoryConvert', analyze: 'tools.categoryAnalyze', recognize: 'tools.categoryRecognize', edit: 'tools.categoryEdit' }
const categoryOptions = computed(() => [{ label: t('plugins.allCategories'), value: 'all' },
  ...pluginCategories.map(value => ({ label: t(categoryLabels[value]), value })), { label: t('plugins.otherCategory'), value: 'other' }])
const source = ref('')
const showSource = ref(false)
const showLogs = ref(false)
const showUpdate = ref(false)
const updateName = ref('')
const updateRuntime = ref('')
const installDependencies = ref(false)
const updatePlugin = computed(() => snapshot.value?.installed.find(plugin => plugin.name === updateName.value))
const installedNames = computed(() => new Set(snapshot.value?.installed.filter(plugin => plugin.managed).map(plugin => plugin.name) || []))
const catalog = computed(() => (snapshot.value?.catalog || []).filter(plugin =>
  pluginSearchText(plugin).includes(search.value.trim().toLowerCase()) && categoryMatches(plugin.category),
))
const installed = computed(() => (snapshot.value?.installed || []).filter(plugin =>
  pluginSearchText(plugin).includes(search.value.trim().toLowerCase()) && pluginToolCategories(plugin).some(categoryMatches),
))
const removedPreinstalled = computed(() => (snapshot.value?.preinstalled || []).filter(plugin =>
  !plugin.installed && plugin.name.toLowerCase().includes(search.value.trim().toLowerCase()),
))
const updateCount = computed(() => Object.values(updates.value).filter(update => update.up_to_date === false).length)
const disabled = computed(() => busy.value || loading.value || catalogLoading.value)

function pluginActivity(name: string) {
  return [...activities.value].reverse().find(task => task.name === name)
}
function installing(name: string) {
  const task = pluginActivity(name)
  return task?.status === 'running' || task?.status === 'cancelling'
}
function installDisabled(name = '') {
  return exclusiveBusy.value || loading.value || catalogLoading.value || (name ? installing(name) : false)
}
function installLabel(name: string) {
  return installing(name) ? t(pluginActivity(name)?.phase === 'queued' ? 'plugins.phases.queued' : 'plugins.installing') : t('plugins.install')
}

onMounted(openMarketplace)
watch(() => [app.runtimeInfo?.backend, app.runtimeInfo?.installState?.pythonPath], () => { if (!busy.value) void openMarketplace(true) })

async function perform(action: PluginAction, name?: string, target?: string, options: { installDependencies?: boolean; expectedRuntime?: string } = {}) {
  if (action === 'install' ? installDisabled(name || target) : disabled.value) { message.warning(t('plugins.busyRetry')); return }
  try {
    const operation = manage(action, { name, target, locale: locale.value, ...options })
    await operation
    message.success(t('plugins.actionComplete'))
  } catch (failure) {
    // Worker failures retain their detailed error across navigation.
    if (!error.value && activity.value?.status !== 'cancelled') message.error(String(failure))
  }
}

function install(name?: string, target?: string) {
  if (name && incompatible(snapshot.value?.catalog.find(plugin => plugin.name === name))) return
  const resolved = target?.trim() || name || ''
  if (!resolved) return
  const expectedRuntime = snapshot.value?.runtime.identity
  dialog.info({
    title: t('plugins.installTitle'),
    content: t('plugins.installConfirm', { source: resolved }),
    positiveText: t('plugins.install'), negativeText: t('common.cancel'),
    onPositiveClick: () => {
      showSource.value = false
      source.value = ''
      void perform('install', name, resolved, { expectedRuntime })
    },
  })
}

function uninstall(name: string) {
  const expectedRuntime = snapshot.value?.runtime.identity
  dialog.warning({
    title: t('plugins.uninstall'), content: t('plugins.uninstallConfirm', { name }),
    positiveText: t('plugins.uninstall'), negativeText: t('common.cancel'),
    onPositiveClick: () => perform('uninstall', name, undefined, { expectedRuntime }),
  })
}

async function chooseFolder() {
  const selected = await open({ directory: true, multiple: false, title: t('plugins.chooseFolder') })
  if (typeof selected === 'string') source.value = selected
}

function categoryMatches(value: unknown) {
  return category.value === 'all' || category.value === (normalizePluginCategory(value) || 'other')
}
function categoryLabel(value: unknown) {
  const normalized = normalizePluginCategory(value)
  return t(normalized ? categoryLabels[normalized] : 'plugins.otherCategory')
}
function catalogTitle(plugin: { name: string; locales?: InstalledPlugin['locales'] }) {
  return pluginLocale(plugin.locales, locale.value).title || plugin.name
}
function sourceLabel(url: string) {
  return /^(?:[a-zA-Z]:[\\/]|\/|\\\\|file:)/.test(url) ? t('plugins.localSource') : url.replace('https://github.com/', '')
}
function incompatible(plugin?: { compatibility?: PluginCompatibility }) {
  return plugin?.compatibility?.compatible === false
}

function openPluginView(plugin: { name: string }, view: { id: string; placements?: string[] }) {
  const placements = view.placements || ['tool']
  if (placements.includes('workspace')) {
    void router.push({ name: 'plugin-view', params: { plugin: plugin.name, viewId: view.id } })
  } else {
    void router.push({ name: 'tools', query: { pluginView: `${plugin.name}/${view.id}` } })
  }
}
function pluginTitle(plugin: { name: string; locales?: InstalledPlugin['locales']; contributions?: InstalledPlugin['contributions'] }) {
  return pluginLocale(plugin.locales, locale.value).title || plugin.contributions?.views?.find(view => view.id === 'tool')?.title || plugin.name
}
function pluginDescription(plugin: InstalledPlugin) {
  return pluginLocale(plugin.locales, locale.value).description || plugin.description || ''
}
function operationLabels(plugin: InstalledPlugin) {
  const titles = new Map(Object.entries(plugin.operationTitles || {}))
  for (const [id, title] of Object.entries(pluginLocale(plugin.locales, locale.value).operations || {})) titles.set(id, title)
  return (plugin.operations || []).map(id => ({ id, title: titles.get(id) || id }))
}
function menuIcon(component: Component) { return () => h(NIcon, { component }) }
function moreOptions(plugin: InstalledPlugin): (DropdownOption | DropdownDividerOption)[] {
  return [
    { label: t(plugin.enabled === false ? 'plugins.enable' : 'plugins.disable'), key: plugin.enabled === false ? 'enable' : 'disable', icon: menuIcon(PowerOutline), disabled: disabled.value || (plugin.enabled === false && incompatible(plugin)) },
    { type: 'divider', key: 'availability-divider' },
    { label: t('plugins.update'), key: 'update', icon: menuIcon(ArrowUpCircleOutline), disabled: disabled.value || !plugin.url },
    { label: t('plugins.repair'), key: 'repair', icon: menuIcon(BuildOutline), disabled: disabled.value },
    { type: 'divider', key: 'divider' },
    { label: () => h('span', { style: { color: 'var(--danger, #d86262)' } }, t('plugins.uninstall')), key: 'uninstall', icon: menuIcon(TrashOutline), disabled: disabled.value },
  ]
}
function selectMore(key: string, plugin: { name: string }) {
  if (key === 'update') {
    updateName.value = plugin.name
    updateRuntime.value = snapshot.value?.runtime.identity || ''
    installDependencies.value = false
    showUpdate.value = true
  } else if (key === 'repair') {
    const expectedRuntime = snapshot.value?.runtime.identity
    dialog.warning({
      title: t('plugins.repair'), content: t('plugins.repairConfirm'),
      positiveText: t('plugins.repair'), negativeText: t('common.cancel'),
      onPositiveClick: () => perform('repair', plugin.name, undefined, { expectedRuntime }),
    })
  } else if (key === 'uninstall') uninstall(plugin.name)
  else if (key === 'enable' || key === 'disable') void perform(key, plugin.name)
}

async function confirmUpdate() {
  if (disabled.value || !updateName.value) return
  const name = updateName.value, dependencies = installDependencies.value
  if (!updateRuntime.value || snapshot.value?.runtime.identity !== updateRuntime.value) {
    showUpdate.value = false
    message.warning(t('plugins.updateRuntimeChanged'))
    return
  }
  showUpdate.value = false
  await perform('update', name, undefined, { installDependencies: dependencies, expectedRuntime: updateRuntime.value })
}

async function requestCancel(id?: string) {
  try { await cancel(id) }
  catch (failure) { message.error(String(failure)) }
}
</script>

<template>
  <div class="page plugins-page">
    <div class="page-header-compact plugins-header">
      <div><h1>{{ t('plugins.title') }}</h1><p>{{ t('plugins.subtitle') }}</p></div>
      <div class="plugins-actions">
        <n-button :disabled="!activity && !logs.length" @click="showLogs = true"><template #icon><n-icon :component="TerminalOutline" /></template>{{ t('tasks.logs') }}</n-button>
        <n-button :disabled="installDisabled() || !snapshot" @click="showSource = true">{{ t('plugins.installSource') }}</n-button>
      </div>
    </div>

    <n-alert v-if="error" type="error" class="plugins-notice">{{ error }}</n-alert>
    <n-alert v-if="!snapshot && !loading && /No active runtime/i.test(error)" type="info" class="plugins-notice">
      {{ t('plugins.runtimeRequired') }}
      <router-link to="/settings">{{ t('nav.settings') }}</router-link>
    </n-alert>
    <template v-if="snapshot">
      <n-alert v-if="snapshot.verificationError" type="warning" class="plugins-notice">{{ t('plugins.verificationUnavailable') }} {{ snapshot.verificationError }}</n-alert>
      <n-alert v-if="snapshot.legacyPlugins?.length" type="info" class="plugins-notice">{{ t('plugins.legacyPluginsMoved', { names: snapshot.legacyPlugins.join(', ') }) }}</n-alert>
      <div class="plugins-runtime">
        <span>{{ t('plugins.runtimeScope') }}</span>
        <code :title="snapshot.runtime.pythonPath">{{ snapshot.runtime.pythonPath }}</code>
      </div>
      <div class="plugins-toolbar">
        <n-input v-model:value="search" clearable :placeholder="t('plugins.search')" class="plugins-search">
          <template #prefix><n-icon :component="SearchOutline" /></template>
        </n-input>
        <n-select v-model:value="category" :options="categoryOptions" :aria-label="t('plugins.categoryFilter')" class="plugins-category-filter" />
        <n-button v-if="tab === 'browse'" secondary :loading="loading || catalogLoading" :disabled="busy" @click="openMarketplace(true)"><template #icon><n-icon :component="RefreshOutline" /></template>{{ t('plugins.refreshCatalog') }}</n-button>
        <n-button v-else :loading="activity?.action === 'check_updates' && busy" :disabled="disabled || !snapshot.installed.some(plugin => plugin.managed)" @click="perform('check_updates')">
          {{ t('plugins.checkUpdates') }}<template v-if="updateCount"> ({{ updateCount }})</template>
        </n-button>
        <n-dropdown v-if="tab === 'installed'" trigger="click" :options="[{ label: t('plugins.reverify'), key: 'verify', icon: menuIcon(CheckmarkCircleOutline), disabled }]" @select="refreshInventory(true)"><n-button secondary :loading="loading" :disabled="disabled" :aria-label="t('plugins.moreMarketActions')"><template #icon><n-icon :component="EllipsisHorizontalOutline" /></template></n-button></n-dropdown>
      </div>
      <n-tabs v-model:value="tab" type="line" animated>
        <n-tab-pane name="browse" :tab="t('plugins.browse')">
          <n-alert v-if="snapshot.catalogError" type="warning" class="plugins-notice">
            {{ t('plugins.catalogUnavailable') }} {{ snapshot.catalogError }}
          </n-alert>
          <div v-if="catalog.length" class="plugins-grid">
            <article v-for="plugin in catalog" :key="plugin.name" class="plugin-card">
              <div class="plugin-card-title"><n-icon :component="ExtensionPuzzleOutline" :size="24" /><h2>{{ catalogTitle(plugin) }}</h2><n-tag size="small" :bordered="false">{{ categoryLabel(plugin.category) }}</n-tag></div>
              <n-tag v-if="incompatible(plugin)" type="warning" size="small" class="plugin-compatibility-tag">{{ t('plugins.incompatible') }}</n-tag>
              <p v-if="catalogTitle(plugin) !== plugin.name" class="plugin-id">{{ plugin.name }}</p>
              <p class="plugin-description">{{ pluginLocale(plugin.locales, locale).description || plugin.description }}</p>
              <div class="plugin-tags"><n-tag v-for="tag in (pluginLocale(plugin.locales, locale).tags || plugin.tags)" :key="tag" size="small" :bordered="false">{{ tag }}</n-tag></div>
              <p v-if="incompatible(plugin)" class="plugin-error">{{ plugin.compatibility?.reasons.join('\n') }}</p>
              <div class="plugin-card-footer">
                <span class="plugin-source" :title="plugin.url">{{ sourceLabel(plugin.url) }}</span>
                <n-button v-if="installedNames.has(plugin.name)" secondary :disabled="disabled" @click="tab = 'installed'">{{ t('plugins.installed') }}</n-button>
                <n-button v-else type="primary" :loading="installing(plugin.name)" :disabled="installDisabled(plugin.name) || incompatible(plugin)" @click="install(plugin.name)">{{ installLabel(plugin.name) }}</n-button>
              </div>
              <p v-if="pluginActivity(plugin.name)?.status === 'failed'" class="plugin-error">{{ pluginActivity(plugin.name)?.error }}</p>
            </article>
          </div>
          <div v-else-if="catalogLoading" class="plugins-empty"><n-spin /></div>
          <n-empty v-else-if="!snapshot.catalogError" :description="t('plugins.noMatches')" class="plugins-empty" />
        </n-tab-pane>
        <n-tab-pane name="installed" :tab="`${t('plugins.installed')} (${snapshot.installed.length})`">
          <p class="plugins-help">{{ t('plugins.installedHint') }}</p>
          <div v-if="installed.length" class="plugins-installed">
            <article v-for="plugin in installed" :key="`${plugin.managed}:${plugin.name}`" class="plugin-card plugin-installed-card">
              <div class="plugin-installed-header">
                <div class="plugin-card-title"><n-icon :component="ExtensionPuzzleOutline" :size="20" /><h2>{{ pluginTitle(plugin) }}</h2><n-tag v-if="plugin.preinstalled" size="small">{{ t('plugins.preinstalled') }}</n-tag></div>
                <n-tag v-if="incompatible(plugin)" type="warning" size="small">{{ t('plugins.incompatible') }}</n-tag>
                <n-tag v-else :type="plugin.enabled === false ? 'default' : plugin.loaded == null ? 'warning' : plugin.loaded ? 'success' : 'error'" size="small">{{ plugin.enabled === false ? t('plugins.disabled') : plugin.loaded == null ? t('plugins.notVerified') : plugin.loaded ? t('plugins.loaded') : t('plugins.loadFailed') }}</n-tag>
              </div>
              <p class="plugin-id">{{ plugin.name }}<span v-if="plugin.version"> · {{ plugin.version }}</span></p>
              <p v-if="plugin.resolvedCommit" class="plugin-id">{{ t('plugins.sourceCommit') }} <code :title="plugin.resolvedCommit">{{ plugin.resolvedCommit.slice(0, 12) }}</code></p>
              <div class="plugin-category-list"><n-tag v-for="value in pluginToolCategories(plugin)" :key="value || 'other'" size="small" :bordered="false">{{ categoryLabel(value) }}</n-tag></div>
              <p v-if="pluginDescription(plugin)" class="plugin-description">{{ pluginDescription(plugin) }}</p>
              <p v-if="plugin.error && !incompatible(plugin)" class="plugin-error">{{ plugin.error }}</p>
              <p v-if="incompatible(plugin)" class="plugin-error">{{ plugin.compatibility?.reasons.join('\n') }}</p>
              <p v-if="plugin.dependencyError" class="plugins-help" :title="plugin.dependencyError">{{ t('plugins.optionalBackendHint') }}</p>
              <p v-if="updates[plugin.name]?.error" class="plugin-error">{{ updates[plugin.name]?.error }}</p>
              <p v-if="updates[plugin.name]?.up_to_date === false" class="plugin-update">{{ updates[plugin.name]?.content_changed && updates[plugin.name]?.local_version === updates[plugin.name]?.remote_version ? t('plugins.filesChanged') : t('plugins.updateAvailable', { version: updates[plugin.name]?.remote_version }) }}</p>
              <p v-else-if="updates[plugin.name]?.up_to_date === true" class="plugins-help">{{ t('plugins.upToDate') }}</p>
              <div v-if="plugin.operations?.length" class="plugin-operations">
                <p class="plugins-help">{{ t('plugins.operations') }}</p>
                <ul class="plugin-operation-list"><li v-for="operation in operationLabels(plugin)" :key="operation.id">{{ operation.title }}</li></ul>
              </div>
              <p v-else-if="plugin.loaded && plugin.managed && !plugin.contributions?.views?.length" class="plugins-help">{{ t('plugins.noOperations') }}</p>
              <p v-if="!plugin.managed" class="plugins-help">{{ t('plugins.manuallyInstalled') }}</p>
              <div class="plugin-installed-footer">
                <div v-if="plugin.contributions?.views?.length" class="plugin-views-row">
                  <n-button v-for="(view, index) in plugin.contributions.views" :key="view.id" :type="index === 0 ? 'primary' : 'default'" size="small" :disabled="!plugin.loaded || incompatible(plugin) || busy" @click="openPluginView(plugin, view)">{{ index === 0 ? t('plugins.openTool') : view.title || view.id }}</n-button>
                </div>
                <div v-if="plugin.managed" class="plugin-installed-actions">
                  <n-dropdown trigger="click" :options="moreOptions(plugin)" @select="(key: string) => selectMore(key, plugin)"><n-button size="small" quaternary circle :loading="installing(plugin.name)" :disabled="disabled" :title="t('plugins.more')" :aria-label="`${pluginTitle(plugin)} · ${t('plugins.more')}`"><template #icon><n-icon :component="EllipsisHorizontalOutline" /></template></n-button></n-dropdown>
                </div>
              </div>
            </article>
          </div>
          <n-empty v-else :description="search || category !== 'all' ? t('plugins.noMatches') : t('plugins.noInstalled')" class="plugins-empty" />
          <div v-if="removedPreinstalled.length" class="plugins-installed">
            <p class="plugins-help">{{ t('plugins.restorePreinstalledHint') }}</p>
            <article v-for="plugin in removedPreinstalled" :key="plugin.name" class="plugin-card plugin-card-footer">
              <span>{{ plugin.name }} · {{ plugin.version }}</span>
              <n-button :disabled="disabled" @click="perform('install_preinstalled', plugin.name)">{{ t('plugins.restorePreinstalled') }}</n-button>
            </article>
          </div>
        </n-tab-pane>
      </n-tabs>
    </template>
    <n-spin v-if="loading && !snapshot" class="plugins-empty" />

    <PluginLogDialog v-model:show="showLogs" :records="logRecords" :task="activity" :tasks="activities" @cancel="requestCancel" />

    <n-modal v-model:show="showUpdate" preset="card" :title="t('plugins.updateTitle', { name: updatePlugin ? pluginTitle(updatePlugin) : updateName })" class="plugin-source-modal" style="width: min(560px, calc(100vw - 32px))">
      <p>{{ t('plugins.updateDescription') }}</p>
      <dl class="plugins-update-versions"><div><dt>{{ t('plugins.currentVersion') }}</dt><dd>{{ updatePlugin?.version || t('plugins.unknownVersion') }}</dd></div><div><dt>{{ t('plugins.targetVersion') }}</dt><dd>{{ updates[updateName]?.remote_version || t('plugins.sourceLatest') }}</dd></div></dl>
      <n-checkbox v-model:checked="installDependencies" :disabled="disabled">{{ t('plugins.updateDependencies') }}</n-checkbox>
      <n-alert :type="installDependencies ? 'warning' : 'info'" :bordered="false" class="plugins-update-hint">{{ t(installDependencies ? 'plugins.updateDependenciesHint' : 'plugins.updateFilesHint') }}</n-alert>
      <template #footer><div class="plugins-actions"><n-button @click="showUpdate = false">{{ t('common.cancel') }}</n-button><n-button type="primary" :disabled="disabled" @click="confirmUpdate">{{ t('plugins.update') }}</n-button></div></template>
    </n-modal>

    <n-modal v-model:show="showSource" preset="card" :title="t('plugins.installSource')" class="plugin-source-modal" style="width: min(560px, calc(100vw - 32px))">
      <p class="plugins-help">{{ t('plugins.sourceHint') }}</p>
      <n-input v-model:value="source" :placeholder="t('plugins.sourcePlaceholder')" :disabled="installDisabled()" />
      <template #footer><div class="plugins-actions">
        <n-button :disabled="disabled" @click="chooseFolder"><template #icon><n-icon :component="FolderOpenOutline" /></template>{{ t('plugins.chooseFolder') }}</n-button>
        <n-button type="primary" :disabled="installDisabled(source) || !source.trim()" @click="install(undefined, source)">{{ t('plugins.install') }}</n-button>
      </div></template>
    </n-modal>
  </div>
</template>

<style scoped>
.plugins-page { max-width: 1400px; margin: 0 auto; }
.plugins-header, .plugins-toolbar, .plugins-actions, .plugin-card-title, .plugin-card-footer, .plugin-installed-header { display: flex; align-items: center; gap: 12px; }
.plugins-header, .plugin-card-footer, .plugin-installed-header { justify-content: space-between; }
.plugins-actions { flex-wrap: wrap; }
.plugins-notice { margin-bottom: 16px; }
.plugins-runtime { display: flex; gap: 12px; flex-wrap: wrap; margin: 4px 0 20px; font-size: 12px; opacity: .7; }
.plugins-runtime code { overflow-wrap: anywhere; }
.plugins-toolbar { flex-wrap: wrap; margin-bottom: 12px; }
.plugins-search { flex: 1; min-width: 200px; }
.plugins-category-filter { width: 150px; flex-shrink: 0; }
.plugin-category-list { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
.plugins-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 310px), 1fr)); gap: 16px; }
.plugin-card { border: 1px solid var(--outline); background: var(--surface-1); border-radius: 12px; padding: 20px; min-width: 0; }
.plugins-grid .plugin-card { display: flex; flex-direction: column; }
.plugin-card-title { justify-content: flex-start; flex-wrap: wrap; }
.plugin-compatibility-tag { align-self: flex-start; margin-top: 8px; }
.plugin-card h2 { font-size: 17px; margin: 0; }
.plugin-description { font-size: 13px; line-height: 1.7; flex: 1; opacity: .8; }
.plugin-tags { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 20px; }
.plugin-source { font-size: 11px; opacity: .6; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.plugins-installed { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 330px), 1fr)); gap: 14px; }
.plugins-installed > .plugins-help { grid-column: 1 / -1; }
.plugin-installed-card { display: flex; flex-direction: column; padding: 16px; background: var(--surface-1); border-color: var(--outline); }
.plugin-id { margin: 4px 0 10px; color: var(--on-surface-muted); font: 11px/1.4 var(--font-mono); overflow-wrap: anywhere; }
.plugin-operations .plugins-help { margin: 4px 0 6px; }
.plugin-operation-list { margin: 0; padding-left: 18px; font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.plugin-installed-footer { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-top: auto; padding-top: 18px; flex-wrap: wrap; }
.plugin-installed-actions, .plugin-views-row { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.plugin-installed-actions { margin-left: auto; }
.plugin-version, .plugins-help { opacity: .65; font-size: 12px; }
.plugin-error { color: #d86262; white-space: pre-wrap; overflow-wrap: anywhere; }
.plugin-update { color: #449f77; }
.plugins-empty { display: flex; justify-content: center; padding: 60px 20px; }
.plugins-update-versions { display: flex; flex-wrap: wrap; gap: 16px 28px; font-size: 13px; margin: 20px 0; }.plugins-update-versions > div { min-width: 0; max-width: 100%; }.plugins-update-versions dt { color: var(--on-surface-muted); }.plugins-update-versions dd { margin: 4px 0 0; overflow-wrap: anywhere; }
.plugins-update-hint { margin-top: 16px; }
@media (max-width: 720px) { .plugins-header { align-items: flex-start; flex-direction: column; } .plugin-installed-header { align-items: flex-start; flex-wrap: wrap; } }
</style>
