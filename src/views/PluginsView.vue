<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { open } from '@tauri-apps/plugin-dialog'
import { useDialog, useMessage } from 'naive-ui'
import { ExtensionPuzzleOutline, RefreshOutline, SearchOutline, FolderOpenOutline } from '@vicons/ionicons5'
import { usePluginMarketplace, type PluginAction } from '@/features/plugin-marketplace/composable'
import { useAppStore } from '@/stores/app'

const { t, locale } = useI18n()
const router = useRouter()
const dialog = useDialog()
const message = useMessage()
const app = useAppStore()
const { snapshot, updates, loading, error, busy, logs, currentName, currentAction, refresh, manage, cancel } = usePluginMarketplace()
const tab = ref<'browse' | 'installed'>('browse')
const search = ref('')
const source = ref('')
const showSource = ref(false)
const showLogs = ref(false)
const installedNames = computed(() => new Set(snapshot.value?.installed.filter(plugin => plugin.managed).map(plugin => plugin.name) || []))
const catalog = computed(() => (snapshot.value?.catalog || []).filter(plugin =>
  `${plugin.name} ${plugin.description} ${plugin.tags.join(' ')}`.toLowerCase().includes(search.value.trim().toLowerCase()),
))
const installed = computed(() => (snapshot.value?.installed || []).filter(plugin =>
  plugin.name.toLowerCase().includes(search.value.trim().toLowerCase()),
))
const updateCount = computed(() => Object.values(updates.value).filter(update => update.up_to_date === false).length)
const disabled = computed(() => busy.value || loading.value)

onMounted(refresh)
watch(() => [app.runtimeInfo?.backend, app.runtimeInfo?.installState?.pythonPath], () => { if (!busy.value) void refresh() })

async function perform(action: PluginAction, name?: string, target?: string) {
  if (disabled.value) { message.warning(t('plugins.busyRetry')); return }
  showLogs.value = true
  try {
    await manage(action, { name, target, locale: locale.value })
    message.success(t('plugins.actionComplete'))
    showSource.value = false
    source.value = ''
  } catch (failure) {
    // Worker failures retain their detailed error across navigation.
    if (!error.value) message.error(String(failure))
  }
}

function install(name?: string, target?: string) {
  const resolved = target?.trim() || name || ''
  if (!resolved) return
  dialog.info({
    title: t('plugins.installTitle'),
    content: t('plugins.installConfirm', { source: resolved }),
    positiveText: t('plugins.install'), negativeText: t('common.cancel'),
    onPositiveClick: () => perform('install', name, resolved),
  })
}

function uninstall(name: string) {
  dialog.warning({
    title: t('plugins.uninstall'), content: t('plugins.uninstallConfirm', { name }),
    positiveText: t('plugins.uninstall'), negativeText: t('common.cancel'),
    onPositiveClick: () => perform('uninstall', name),
  })
}

async function chooseFolder() {
  const selected = await open({ directory: true, multiple: false, title: t('plugins.chooseFolder') })
  if (typeof selected === 'string') source.value = selected
}

function runOperations() {
  void router.push({ name: 'tools' })
}

function openPluginView(plugin: { name: string }, view: { id: string; placements?: string[] }) {
  const placements = view.placements || ['tool']
  if (placements.includes('workspace')) {
    void router.push({ name: 'plugin-view', params: { plugin: plugin.name, viewId: view.id } })
  } else {
    void router.push({ name: 'tools', query: { pluginView: `${plugin.name}/${view.id}` } })
  }
}
</script>

<template>
  <div class="page plugins-page">
    <div class="page-header-compact plugins-header">
      <div><h1>{{ t('plugins.title') }}</h1><p>{{ t('plugins.subtitle') }}</p></div>
      <div class="plugins-actions">
        <n-button :disabled="disabled || !snapshot" @click="showSource = true">{{ t('plugins.installSource') }}</n-button>
        <n-button secondary :loading="loading" :disabled="busy" @click="refresh">
          <template #icon><n-icon :component="RefreshOutline" /></template>{{ t('plugins.refresh') }}
        </n-button>
      </div>
    </div>

    <n-alert v-if="error" type="error" class="plugins-notice">{{ error }}</n-alert>
    <n-alert v-if="!snapshot && !loading && /No active runtime/i.test(error)" type="info" class="plugins-notice">
      {{ t('plugins.runtimeRequired') }}
      <router-link to="/settings">{{ t('nav.settings') }}</router-link>
    </n-alert>
    <template v-if="snapshot">
      <n-alert v-if="snapshot.verificationError" type="warning" class="plugins-notice">{{ t('plugins.verificationUnavailable') }} {{ snapshot.verificationError }}</n-alert>
      <div class="plugins-runtime">
        <span>{{ t('plugins.runtimeScope') }}</span>
        <code :title="snapshot.runtime.pythonPath">{{ snapshot.runtime.pythonPath }}</code>
      </div>
      <div class="plugins-toolbar">
        <n-input v-model:value="search" clearable :placeholder="t('plugins.search')" class="plugins-search">
          <template #prefix><n-icon :component="SearchOutline" /></template>
        </n-input>
        <n-button :disabled="disabled || !snapshot.installed.some(plugin => plugin.managed)" @click="perform('check_updates')">
          {{ t('plugins.checkUpdates') }}<template v-if="updateCount"> ({{ updateCount }})</template>
        </n-button>
        <n-button :disabled="disabled" @click="runOperations">{{ t('plugins.runOperations') }}</n-button>
      </div>
      <n-tabs v-model:value="tab" type="line" animated>
        <n-tab-pane name="browse" :tab="t('plugins.browse')">
          <n-alert v-if="snapshot.catalogError" type="warning" class="plugins-notice">
            {{ t('plugins.catalogUnavailable') }} {{ snapshot.catalogError }}
          </n-alert>
          <div v-if="catalog.length" class="plugins-grid">
            <article v-for="plugin in catalog" :key="plugin.name" class="plugin-card">
              <div class="plugin-card-title"><n-icon :component="ExtensionPuzzleOutline" :size="24" /><h2>{{ plugin.name }}</h2></div>
              <p class="plugin-description">{{ plugin.description }}</p>
              <div class="plugin-tags"><n-tag v-for="tag in plugin.tags" :key="tag" size="small" :bordered="false">{{ tag }}</n-tag></div>
              <div class="plugin-card-footer">
                <span class="plugin-source" :title="plugin.url">{{ plugin.url.replace('https://github.com/', '') }}</span>
                <n-button v-if="installedNames.has(plugin.name)" secondary :disabled="disabled" @click="tab = 'installed'">{{ t('plugins.installed') }}</n-button>
                <n-button v-else type="primary" :disabled="disabled" @click="install(plugin.name)">{{ t('plugins.install') }}</n-button>
              </div>
            </article>
          </div>
          <n-empty v-else-if="!snapshot.catalogError" :description="t('plugins.noMatches')" class="plugins-empty" />
        </n-tab-pane>
        <n-tab-pane name="installed" :tab="`${t('plugins.installed')} (${snapshot.installed.length})`">
          <p class="plugins-help">{{ t('plugins.installedHint') }}</p>
          <div v-if="installed.length" class="plugins-installed">
            <article v-for="plugin in installed" :key="`${plugin.managed}:${plugin.name}`" class="plugin-card">
              <div class="plugin-installed-header">
                <div class="plugin-card-title"><n-icon :component="ExtensionPuzzleOutline" :size="24" /><h2>{{ plugin.name }}</h2><span v-if="plugin.version" class="plugin-version">{{ plugin.version }}</span></div>
                <n-tag :type="plugin.loaded == null ? 'warning' : plugin.loaded ? 'success' : 'error'" size="small">{{ plugin.loaded == null ? t('plugins.notVerified') : plugin.loaded ? t('plugins.loaded') : t('plugins.loadFailed') }}</n-tag>
              </div>
              <p v-if="plugin.error" class="plugin-error">{{ plugin.error }}</p>
              <p v-if="updates[plugin.name]?.error" class="plugin-error">{{ updates[plugin.name]?.error }}</p>
              <p v-if="updates[plugin.name]?.up_to_date === false" class="plugin-update">{{ t('plugins.updateAvailable', { version: updates[plugin.name]?.remote_version }) }}</p>
              <p v-else-if="updates[plugin.name]?.up_to_date === true" class="plugins-help">{{ t('plugins.upToDate') }}</p>
              <p v-if="plugin.operations?.length" class="plugins-help">{{ t('plugins.operationCount', { count: plugin.operations?.length }) }}</p>
              <p v-else-if="plugin.loaded && plugin.managed" class="plugins-help">{{ t('plugins.noOperations') }}</p>
              <code class="plugin-location">{{ plugin.path }}</code>
              <p v-if="!plugin.managed" class="plugins-help">{{ t('plugins.manuallyInstalled') }}</p>
              <div v-if="plugin.contributions?.views?.length" class="plugin-views-row">
                <n-button
                  v-for="view in plugin.contributions.views"
                  :key="view.id"
                  size="small"
                  secondary
                  :disabled="!plugin.loaded"
                  @click="openPluginView(plugin, view)"
                >{{ t('plugins.openView', { title: view.title || view.id }) }}</n-button>
              </div>
              <div v-if="plugin.managed" class="plugin-card-footer plugin-installed-actions">
                <n-button v-if="plugin.url" :disabled="disabled" @click="perform('update', plugin.name)">{{ t('plugins.update') }}</n-button>
                <n-button :disabled="disabled" @click="perform('repair', plugin.name)">{{ t('plugins.repair') }}</n-button>
                <n-button type="error" secondary :disabled="disabled" @click="uninstall(plugin.name)">{{ t('plugins.uninstall') }}</n-button>
              </div>
            </article>
          </div>
          <n-empty v-else :description="search ? t('plugins.noMatches') : t('plugins.noInstalled')" class="plugins-empty" />
          <p class="plugins-directory">{{ snapshot.pluginsDir }}</p>
        </n-tab-pane>
      </n-tabs>
    </template>
    <n-spin v-if="loading && !snapshot" class="plugins-empty" />

    <section v-if="busy || logs.length" class="plugins-log-panel">
      <div class="plugins-log-header">
        <span>{{ busy ? t('plugins.working', { name: currentName || (currentAction === 'check_updates' ? t('plugins.checkUpdates') : '') }) : t('plugins.operationLog') }}</span>
        <div class="plugins-actions"><n-button text @click="showLogs = !showLogs">{{ showLogs ? t('plugins.hideLog') : t('plugins.showLog') }}</n-button><n-button v-if="busy" size="small" @click="cancel().catch(failure => message.error(String(failure)))">{{ t('common.cancel') }}</n-button></div>
      </div>
      <n-progress v-if="busy" type="line" status="info" :percentage="100" :show-indicator="false" processing />
      <pre v-if="showLogs" class="plugins-log">{{ logs.join('\n') }}</pre>
    </section>

    <n-modal v-model:show="showSource" preset="card" :title="t('plugins.installSource')" class="plugin-source-modal" style="width: min(560px, calc(100vw - 32px))">
      <p class="plugins-help">{{ t('plugins.sourceHint') }}</p>
      <n-input v-model:value="source" :placeholder="t('plugins.sourcePlaceholder')" :disabled="disabled" />
      <template #footer><div class="plugins-actions">
        <n-button :disabled="disabled" @click="chooseFolder"><template #icon><n-icon :component="FolderOpenOutline" /></template>{{ t('plugins.chooseFolder') }}</n-button>
        <n-button type="primary" :disabled="disabled || !source.trim()" @click="install(undefined, source)">{{ t('plugins.install') }}</n-button>
      </div></template>
    </n-modal>
  </div>
</template>

<style scoped>
.plugins-page { max-width: 1400px; margin: 0 auto; }
.plugins-header, .plugins-toolbar, .plugins-actions, .plugin-card-title, .plugin-card-footer, .plugin-installed-header, .plugins-log-header { display: flex; align-items: center; gap: 12px; }
.plugins-header, .plugin-card-footer, .plugin-installed-header, .plugins-log-header { justify-content: space-between; }
.plugins-actions { flex-wrap: wrap; }
.plugins-notice { margin-bottom: 16px; }
.plugins-runtime { display: flex; gap: 12px; flex-wrap: wrap; margin: 4px 0 20px; font-size: 12px; opacity: .7; }
.plugins-runtime code { overflow-wrap: anywhere; }
.plugins-toolbar { flex-wrap: wrap; margin-bottom: 12px; }
.plugins-search { flex: 1; min-width: 200px; }
.plugins-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 310px), 1fr)); gap: 16px; }
.plugin-card { border: 1px solid var(--border-color, rgba(128,128,128,.2)); background: var(--bg-elevated, rgba(128,128,128,.04)); border-radius: 12px; padding: 20px; min-width: 0; }
.plugins-grid .plugin-card { display: flex; flex-direction: column; }
.plugin-card-title { justify-content: flex-start; flex-wrap: wrap; }
.plugin-card h2 { font-size: 17px; margin: 0; }
.plugin-description { font-size: 13px; line-height: 1.7; flex: 1; opacity: .8; }
.plugin-tags { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 20px; }
.plugin-source { font-size: 11px; opacity: .6; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.plugins-installed { display: grid; gap: 14px; }
.plugin-installed-actions { justify-content: flex-end; margin-top: 16px; flex-wrap: wrap; }
.plugin-version, .plugins-help { opacity: .65; font-size: 12px; }
.plugin-error { color: #d86262; white-space: pre-wrap; overflow-wrap: anywhere; }
.plugin-update { color: #449f77; }
.plugin-location, .plugins-directory { font-size: 11px; opacity: .6; overflow-wrap: anywhere; }
.plugins-empty { display: flex; justify-content: center; padding: 60px 20px; }
.plugins-log-panel { margin-top: 24px; border-top: 1px solid rgba(128,128,128,.2); padding-top: 16px; }
.plugins-log-header { margin-bottom: 10px; font-size: 13px; }
.plugins-log { max-height: 250px; overflow: auto; font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; background: rgba(128,128,128,.08); border-radius: 8px; }
@media (max-width: 720px) { .plugins-header { align-items: flex-start; flex-direction: column; } .plugin-installed-header { align-items: flex-start; flex-wrap: wrap; } }
</style>
