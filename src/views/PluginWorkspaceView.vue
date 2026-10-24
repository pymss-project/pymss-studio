<script setup lang="ts">
/**
 * Plugin workspace view: opens a plugin-contributed page (protocol v1 view
 * with placement "workspace") as a full page, e.g. a dedicated editor for one
 * plugin's DAG. Entry: /plugin-view/:plugin/:viewId
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { invoke } from '@tauri-apps/api/core'
import { usePluginMarketplace } from '@/features/plugin-marketplace/composable'
import PluginViewHost from '@/features/plugin-marketplace/PluginViewHost.vue'

const route = useRoute()
const { t } = useI18n()
const pluginMarket = usePluginMarketplace()
const loading = ref(true)

const pluginName = computed(() => String(route.params.plugin || ''))
const viewId = computed(() => String(route.params.viewId || ''))

const entry = computed(() => {
  const plugin = pluginMarket.snapshot.value?.installed.find(p => p.name === pluginName.value)
  const view = plugin?.contributions?.views?.find(v => v.id === viewId.value)
  return plugin && view ? { plugin, view } : null
})

onMounted(async () => {
  if (!pluginMarket.snapshot.value) {
    await pluginMarket.refresh().catch(() => undefined)
  }
  loading.value = false
  // Keep the protocol handler pointed at the active runtime's plugins dir.
  const pluginsDir = pluginMarket.snapshot.value?.pluginsDir
  if (pluginsDir) await invoke('set_plugin_view_root', { root: pluginsDir }).catch(() => undefined)
})
</script>

<template>
  <div class="page plugin-workspace-page">
    <div class="page-header-compact">
      <div>
        <h1>{{ entry?.view.title || viewId }}</h1>
        <p>{{ entry ? t('plugins.workspaceSubtitle', { plugin: entry.view.title || viewId }) : '' }}</p>
      </div>
    </div>
    <n-alert v-if="!loading && !entry" type="warning">{{ t('plugins.workspaceMissing') }}</n-alert>
    <PluginViewHost
      v-if="entry"
      :key="`${pluginName}/${viewId}`"
      :plugin-name="pluginName"
      :view="entry.view"
    />
  </div>
</template>

<style scoped>
.plugin-workspace-page { display: flex; flex-direction: column; gap: 12px; height: 100%; }
</style>
