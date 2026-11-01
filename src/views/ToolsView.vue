<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter, type LocationQueryRaw } from 'vue-router'
import {
  ExtensionPuzzleOutline, SwapHorizontalOutline, InformationCircleOutline, AnalyticsOutline,
  CutOutline, GitMergeOutline, VolumeHighOutline, MicOutline, MusicalNoteOutline, PulseOutline,
} from '@vicons/ionicons5'
import { loadPluginToolState, updatePluginToolState } from '@/features/plugin-marketplace/toolState'
import { usePluginMarketplace } from '@/features/plugin-marketplace/composable'
import PluginViewHost from '@/features/plugin-marketplace/PluginViewHost.vue'
import { pluginToolEntries, type PluginToolEntry } from '@/features/plugin-marketplace/toolEntries'
import { pluginLocale } from '@/features/plugin-marketplace/presentation'
import { recordFeatureVisit } from '@/utils/logger'
import { useAppStore } from '@/stores/app'
import { activeRuntimeEnvironment } from '@/utils/runtime'
import { useSettingsStore } from '@/stores/settings'

defineOptions({ name: 'ToolsView' })

const { t, locale } = useI18n()
const route = useRoute()
const router = useRouter()
const pluginMarket = usePluginMarketplace()
const app = useAppStore()
const settings = useSettingsStore()
const preparingTool = ref(false)
const selectedPreparationSource = ref<string | null>(null)

const activePluginTool = ref('')
const mountedPluginTool = ref<{ entry: PluginToolEntry; pluginsRoot: string } | null>(null)
const pluginToolBusy = ref(false)
const unavailableToolId = ref('')
const restored = ref(false)
const inventoryLoading = computed(() => !restored.value || pluginMarket.loading.value)
let lastVisitedTool = ''

const categoryDefinitions = [
  { id: 'convert', titleKey: 'tools.categoryConvert' },
  { id: 'analyze', titleKey: 'tools.categoryAnalyze' },
  { id: 'recognize', titleKey: 'tools.categoryRecognize' },
  { id: 'edit', titleKey: 'tools.categoryEdit' },
]

const pluginTools = computed(() => pluginToolEntries(pluginMarket.snapshot.value?.installed || []))
const categories = computed(() => categoryDefinitions.filter(category => pluginTools.value.some(entry => entry.category === category.id)))
const categorizedTools = (category: string) => pluginTools.value.filter(entry => entry.category === category)
const uncategorizedTools = computed(() => pluginTools.value.filter(entry => !entry.category))
const hasInstalledPlugins = computed(() => pluginMarket.snapshot.value?.installed.some(plugin => plugin.managed))
const switchingDisabled = computed(() => pluginToolBusy.value && Boolean(activePluginTool.value))
const mobileToolOptions = computed(() => pluginTools.value.map(entry => ({ label: toolLabel(entry), value: entry.key })))

function resolveActivePluginHost(
  entries: readonly PluginToolEntry[],
  key: string,
  busy: boolean,
  mounted: { entry: PluginToolEntry; pluginsRoot: string } | null,
  pluginsRoot: string,
) {
  if (busy && mounted?.entry.key === key) return mounted
  const entry = entries.find(item => item.key === key)
  return entry ? { entry, pluginsRoot } : null
}

const activePluginHost = computed(() => resolveActivePluginHost(
  pluginTools.value,
  activePluginTool.value,
  pluginToolBusy.value,
  mountedPluginTool.value,
  pluginMarket.snapshot.value?.pluginsDir || '',
))
const mobileSelectedValue = computed(() => activePluginTool.value || undefined)

function toolLabel(entry: PluginToolEntry) {
  return pluginLocale(entry.locales, locale.value).title || entry.view.title || entry.view.id
}

function toolIcon(entry: PluginToolEntry) {
  const icons = {
    SwapHorizontalOutline, InformationCircleOutline, AnalyticsOutline, CutOutline,
    GitMergeOutline, VolumeHighOutline, MicOutline, MusicalNoteOutline, PulseOutline,
  }
  return icons[entry.icon as keyof typeof icons] || ExtensionPuzzleOutline
}

function replaceQuery(query: LocationQueryRaw) {
  void router.replace({ query })
}

function persistPluginSelection(entry: PluginToolEntry) {
  if (!restored.value) return
  void updatePluginToolState({ activePluginTool: entry.routeId })
}

function activatePluginTool(entry: PluginToolEntry, replaceRoute = true) {
  if (switchingDisabled.value && activePluginTool.value) return
  unavailableToolId.value = ''
  activePluginTool.value = entry.key
  mountedPluginTool.value = { entry, pluginsRoot: pluginMarket.snapshot.value?.pluginsDir || '' }
  if (replaceRoute) replaceQuery({ pluginView: entry.routeId })
  persistPluginSelection(entry)
  if (restored.value && entry.routeId !== lastVisitedTool) {
    lastVisitedTool = entry.routeId
    recordFeatureVisit('tools', entry.routeId)
  }
}

function markUnavailable(routeId: string) {
  activePluginTool.value = ''
  mountedPluginTool.value = null
  unavailableToolId.value = routeId
}

function applyRequestedPluginView() {
  const routeId = route.query.pluginView
  const entry = typeof routeId === 'string' ? pluginTools.value.find(item => item.routeId === routeId) : null
  if (entry) activatePluginTool(entry, false)
  else if (!(pluginToolBusy.value && activePluginHost.value)) {
    activePluginTool.value = ''
    mountedPluginTool.value = null
    if (typeof routeId === 'string') markUnavailable(routeId)
  }
}

function selectMobileTool(value: string) {
  const entry = pluginTools.value.find(item => item.key === value)
  if (entry) activatePluginTool(entry)
}

function openPlugins() {
  void router.push('/plugins')
}

watch(activePluginTool, () => { selectedPreparationSource.value = null })

async function prepareActiveTool() {
  const entry = activePluginHost.value?.entry
  if (!entry || preparingTool.value) return
  const source = entry.sourceSelectionRequired ? entry.availableBindings?.find(item => item.runtime === selectedPreparationSource.value) : undefined
  if (entry.sourceSelectionRequired && !source) return
  preparingTool.value = true
  try {
    await pluginMarket.manage('prepare', {
      name: entry.pluginName, expectedRuntime: pluginMarket.snapshot.value?.runtime.identity,
      ...(source ? { sourceRuntime: source.runtime, sourceVersion: source.version, sourceContent: source.content } : {}),
    })
  } catch { /* The shared plugin activity retains the failure and its details. */ }
  finally { preparingTool.value = false }
}

async function enableDefaultTools() {
  settings.pluginDefaultsPolicy = 'auto'
  try { await pluginMarket.manage('prepare_preinstalled', { expectedRuntime: pluginMarket.snapshot.value?.runtime.identity }) }
  catch { /* The shared plugin activity retains the failure. */ }
}

function onPluginBusy(value: boolean) {
  pluginToolBusy.value = value
  if (value) return
  mountedPluginTool.value = null
  if (activePluginTool.value && !pluginTools.value.some(entry => entry.key === activePluginTool.value)) {
    markUnavailable(String(route.query.pluginView || ''))
  }
  if (typeof route.query.pluginView === 'string') applyRequestedPluginView()
}

function restoreStoredTool(stored: { activePluginTool?: string } | null) {
  const entry = stored?.activePluginTool ? pluginTools.value.find(item => item.routeId === stored.activePluginTool) : undefined
  if (entry) activatePluginTool(entry, false)
  return Boolean(entry)
}

watch([() => route.query.pluginView, pluginTools], () => {
  if (typeof route.query.pluginView === 'string') applyRequestedPluginView()
  else if (unavailableToolId.value) markUnavailable('')
}, { immediate: true })

watch(pluginTools, entries => {
  if (!restored.value || pluginToolBusy.value || activePluginHost.value) return
  const requested = route.query.pluginView
  const entry = entries.find(item => item.category === 'convert') || entries[0]
  if (typeof requested === 'string') markUnavailable(requested)
  else if (entry) activatePluginTool(entry, false)
  else markUnavailable('')
}, { immediate: true })

watch([() => route.name, () => activeRuntimeEnvironment(app.runtimeInfo)?.pythonPath],
  ([name, pythonPath], [previousName, previousPythonPath]) => {
    if (!restored.value || pluginToolBusy.value) return
    if (pythonPath !== previousPythonPath) {
      void pluginMarket.reloadRuntimeInventory()
    } else if (name === 'tools' && previousName !== 'tools') {
      void pluginMarket.refreshInventory()
    }
  },
)

onMounted(async () => {
  try {
    const [, stored] = await Promise.all([pluginMarket.ensureInventory(), loadPluginToolState()])
    const restoredStored = restoreStoredTool(stored)
    if (typeof route.query.pluginView === 'string') applyRequestedPluginView()
    else if (!restoredStored) {
      const defaultEntry = pluginTools.value.find(entry => entry.category === 'convert') || pluginTools.value[0]
      if (defaultEntry) activatePluginTool(defaultEntry, false)
    }
  } finally {
    restored.value = true
  }
})
</script>

<template>
  <div class="page tools-page audio-tools-page">
    <div class="page-header-compact">
      <div><h1>{{ t('tools.title') }}</h1><p>{{ t('tools.subtitle') }}</p></div>
    </div>

    <div v-if="!inventoryLoading && !pluginTools.length && !unavailableToolId" class="plugin-tool-empty">
      <n-alert v-if="pluginMarket.error.value" type="warning" :bordered="false">{{ pluginMarket.error.value }}</n-alert>
      <n-empty :description="t(hasInstalledPlugins ? 'tools.noAvailableTools' : 'tools.noInstalledTools')">
        <template #extra>
          <p>{{ t(hasInstalledPlugins ? 'tools.enableToolsHint' : 'tools.installToolsHint') }}</p>
          <n-space justify="center">
            <n-button v-if="pluginMarket.snapshot.value?.preinstalledAvailable?.length" type="primary" :loading="pluginMarket.busy.value" @click="enableDefaultTools">{{ t('plugins.enableDefaultTools') }}</n-button>
            <n-button :type="pluginMarket.snapshot.value?.preinstalledAvailable?.length ? 'default' : 'primary'" @click="openPlugins">{{ t('tools.browseOfficialPlugins') }}</n-button>
          </n-space>
        </template>
      </n-empty>
    </div>
    <template v-else>
    <n-select
      :value="mobileSelectedValue"
      :disabled="switchingDisabled"
      :options="mobileToolOptions"
      class="audio-tools-mobile-select"
      data-guide="audio-tools"
      @update:value="selectMobileTool"
    />

    <div class="audio-tools-workspace">
      <nav class="audio-tools-nav" :aria-label="t('tools.toolNavigation')">
        <section v-for="category in categories" :key="category.id">
          <strong>{{ t(category.titleKey) }}</strong>
          <button
            v-for="entry in categorizedTools(category.id)"
            :key="entry.key"
            type="button"
            :disabled="switchingDisabled && activePluginTool !== entry.key"
            :class="{ active: activePluginTool === entry.key }"
            @click="activatePluginTool(entry)"
          >
            <n-icon :component="toolIcon(entry)" />
            <span>{{ toolLabel(entry) }}</span>
          </button>
        </section>
        <section v-if="uncategorizedTools.length">
          <strong>{{ t('tools.categoryPlugins') }}</strong>
          <button
            v-for="entry in uncategorizedTools"
            :key="entry.key"
            type="button"
            :disabled="switchingDisabled && activePluginTool !== entry.key"
            :class="{ active: activePluginTool === entry.key }"
            @click="activatePluginTool(entry)"
          >
            <n-icon :component="toolIcon(entry)" />
            <span>{{ toolLabel(entry) }}</span>
          </button>
        </section>
      </nav>

      <main class="audio-tools-content">
        <div v-if="inventoryLoading" class="plugin-tool-loading">
          <n-spin />
          <p v-if="pluginMarket.logs.value.at(-1)">{{ pluginMarket.logs.value.at(-1) }}</p>
        </div>
        <div v-else-if="unavailableToolId" class="plugin-tool-unavailable">
          <p>{{ t('tools.pluginToolUnavailable') }}</p>
          <n-button secondary @click="openPlugins">{{ t('nav.plugins') }}</n-button>
        </div>
        <div v-show="!inventoryLoading && !unavailableToolId">
          <n-alert v-if="pluginMarket.error.value" type="warning" :bordered="false">{{ pluginMarket.error.value }}</n-alert>
          <PluginViewHost
            v-if="activePluginHost && (!activePluginHost.entry.availability || activePluginHost.entry.availability === 'ready')"
            :key="`${activePluginHost.pluginsRoot}/${activePluginTool}/${activePluginHost.entry.view.revision || ''}`"
            :plugin-name="activePluginHost.entry.pluginName"
            :plugins-root="activePluginHost.pluginsRoot"
            :view="activePluginHost.entry.view"
            @busy-change="onPluginBusy"
          />
          <section v-else-if="activePluginHost" class="plugin-tool-unavailable">
            <h2>{{ toolLabel(activePluginHost.entry) }}</h2>
            <p>{{ t(activePluginHost.entry.availability === 'not_prepared' ? 'plugins.prepareRuntimeHint' : 'plugins.unavailableRuntimeHint') }}</p>
            <p v-if="activePluginHost.entry.error">{{ activePluginHost.entry.error }}</p>
            <template v-if="activePluginHost.entry.availability === 'not_prepared' && activePluginHost.entry.availableBindings?.length">
              <n-select v-if="activePluginHost.entry.sourceSelectionRequired" v-model:value="selectedPreparationSource"
                :placeholder="t('plugins.chooseSourceVersion')" :disabled="preparingTool"
                :options="activePluginHost.entry.availableBindings.map(item => ({ value: item.runtime, label: `${item.runtime} · ${item.version || '—'}` }))" />
              <p>{{ t('plugins.prepareDependenciesHint') }}</p>
              <n-button type="primary" :loading="preparingTool" :disabled="pluginMarket.busy.value || (activePluginHost.entry.sourceSelectionRequired && !selectedPreparationSource)" @click="prepareActiveTool">{{ t('plugins.prepareRuntime') }}</n-button>
            </template>
            <p v-else-if="activePluginHost.entry.availability === 'not_prepared'">{{ t('plugins.prepareSourceMissing') }}</p>
            <n-button secondary @click="openPlugins">{{ t('nav.plugins') }}</n-button>
          </section>
          <n-empty v-else :description="t('tools.pluginToolUnavailable')">
            <template #extra><n-button secondary @click="openPlugins">{{ t('nav.plugins') }}</n-button></template>
          </n-empty>
        </div>
      </main>
    </div>
    </template>
  </div>
</template>

<style src="@/features/plugin-marketplace/tools.css"></style>
