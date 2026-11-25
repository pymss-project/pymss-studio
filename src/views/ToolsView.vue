<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter, type LocationQueryRaw } from 'vue-router'
import {
  ExtensionPuzzleOutline, SwapHorizontalOutline, InformationCircleOutline, AnalyticsOutline,
  CutOutline, GitMergeOutline, VolumeHighOutline, MicOutline, MusicalNoteOutline, PulseOutline,
  ChevronDownOutline,
} from '@vicons/ionicons5'
import { loadPluginToolState, updatePluginToolState } from '@/features/plugin-marketplace/toolState'
import { usePluginMarketplace } from '@/features/plugin-marketplace/composable'
import PluginViewHost from '@/features/plugin-marketplace/PluginViewHost.vue'
import { pluginToolEntries, type PluginToolEntry } from '@/features/plugin-marketplace/toolEntries'
import { reconcileMountedTools, type MountedTool } from '@/features/plugin-marketplace/toolInstances'
import { pluginLocale } from '@/features/plugin-marketplace/presentation'
import { recordFeatureVisit } from '@/utils/logger'
import { useSettingsStore } from '@/stores/settings'

defineOptions({ name: 'ToolsView' })

const { t, locale } = useI18n()
const route = useRoute()
const router = useRouter()
const pluginMarket = usePluginMarketplace()
const settings = useSettingsStore()
const preparingTool = ref(false)
const selectedPreparationSource = ref<string | null>(null)

// Recently used tools stay mounted so switching back is immediate and running tasks survive
// a switch. A busy instance is never evicted and keeps its entry until its task settles.
const MAX_MOUNTED_TOOLS = 4

const activePluginTool = ref('')
const toolInstances = ref<MountedTool<PluginToolEntry>[]>([])
let instanceUseCounter = 0
const unavailableToolId = ref('')
const restored = ref(false)
const collapsedToolCategories = ref<string[]>([])
// Background refreshes keep the current tool on screen; only a missing inventory shows the spinner.
const inventoryLoading = computed(() => !restored.value || (pluginMarket.loading.value && !pluginMarket.snapshot.value))
let lastVisitedTool = ''

const categoryDefinitions = [
  { id: 'convert', titleKey: 'tools.categoryConvert' },
  { id: 'analyze', titleKey: 'tools.categoryAnalyze' },
  { id: 'recognize', titleKey: 'tools.categoryRecognize' },
  { id: 'edit', titleKey: 'tools.categoryEdit' },
]

const pluginTools = computed(() => pluginToolEntries(pluginMarket.snapshot.value?.installed || []))
const categories = computed(() => {
  const groups = categoryDefinitions.map(category => ({
    ...category, entries: pluginTools.value.filter(entry => entry.category === category.id),
  })).filter(category => category.entries.length)
  const otherTools = pluginTools.value.filter(entry => !entry.category)
  if (otherTools.length) groups.push({ id: 'other', titleKey: 'tools.categoryPlugins', entries: otherTools })
  return groups
})
const hasInstalledPlugins = computed(() => pluginMarket.snapshot.value?.installed.some(plugin => plugin.managed))
const pluginsRoot = computed(() => pluginMarket.snapshot.value?.pluginsDir || '')
const anyToolBusy = computed(() => toolInstances.value.some(instance => instance.busy))
const mobileToolOptions = computed(() => pluginTools.value.map(entry => ({ label: toolLabel(entry), value: entry.key })))

function isReadyTool(entry: PluginToolEntry) {
  return !entry.availability || entry.availability === 'ready'
}

function toolInstanceId(entry: PluginToolEntry, root: string) {
  return `${root}/${entry.key}/${entry.view.revision || ''}`
}

const runningActiveInstance = computed(() =>
  toolInstances.value.find(instance => instance.busy && instance.entry.key === activePluginTool.value))
const activePluginHost = computed(() => {
  const running = runningActiveInstance.value
  if (running) return { entry: running.entry, pluginsRoot: running.pluginsRoot }
  const entry = pluginTools.value.find(item => item.key === activePluginTool.value)
  return entry ? { entry, pluginsRoot: pluginsRoot.value } : null
})
const shownInstanceId = computed(() => {
  if (runningActiveInstance.value) return runningActiveInstance.value.id
  const host = activePluginHost.value
  return host && isReadyTool(host.entry) && host.pluginsRoot ? toolInstanceId(host.entry, host.pluginsRoot) : ''
})
const mobileSelectedValue = computed(() => activePluginTool.value || undefined)

function syncToolInstances() {
  const root = pluginsRoot.value
  const currentIds = new Set(pluginTools.value.filter(isReadyTool).map(entry => toolInstanceId(entry, root)))
  const host = activePluginHost.value
  const shown = shownInstanceId.value && host ? { id: shownInstanceId.value, ...host } : null
  toolInstances.value = reconcileMountedTools(toolInstances.value, currentIds, shown, MAX_MOUNTED_TOOLS, ++instanceUseCounter)
}

watch([pluginTools, pluginsRoot, shownInstanceId], syncToolInstances, { immediate: true })

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

function toggleToolCategory(category: string) {
  collapsedToolCategories.value = collapsedToolCategories.value.includes(category)
    ? collapsedToolCategories.value.filter(id => id !== category)
    : [...collapsedToolCategories.value, category]
  void updatePluginToolState({ collapsedToolCategories: [...collapsedToolCategories.value] })
}

function replaceQuery(query: LocationQueryRaw) {
  void router.replace({ query })
}

function persistPluginSelection(entry: PluginToolEntry) {
  if (!restored.value) return
  void updatePluginToolState({ activePluginTool: entry.routeId })
}

function activatePluginTool(entry: PluginToolEntry, replaceRoute = true) {
  unavailableToolId.value = ''
  activePluginTool.value = entry.key
  if (replaceRoute) replaceQuery({ pluginView: entry.routeId })
  persistPluginSelection(entry)
  if (restored.value && entry.routeId !== lastVisitedTool) {
    lastVisitedTool = entry.routeId
    recordFeatureVisit('tools', entry.routeId)
  }
}

function markUnavailable(routeId: string) {
  activePluginTool.value = ''
  unavailableToolId.value = routeId
}

function applyRequestedPluginView() {
  const routeId = route.query.pluginView
  const entry = typeof routeId === 'string' ? pluginTools.value.find(item => item.routeId === routeId) : null
  if (entry) activatePluginTool(entry, false)
  else if (!runningActiveInstance.value) {
    activePluginTool.value = ''
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

function onPluginBusy(instanceId: string, value: boolean) {
  const instance = toolInstances.value.find(item => item.id === instanceId)
  if (!instance || instance.busy === value) return
  instance.busy = value
  if (value) return
  syncToolInstances()
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
  if (!restored.value || activePluginHost.value) return
  const requested = route.query.pluginView
  const entry = entries.find(item => item.category === 'convert') || entries[0]
  if (typeof requested === 'string') markUnavailable(requested)
  else if (entry) activatePluginTool(entry, false)
  else markUnavailable('')
}, { immediate: true })

watch(() => route.name,
  (name, previousName) => {
    if (!restored.value || anyToolBusy.value) return
    if (name === 'tools' && previousName !== 'tools') {
      void pluginMarket.refreshInventory()
    }
  },
)

onMounted(async () => {
  try {
    const [, stored] = await Promise.all([pluginMarket.ensureInventory(), loadPluginToolState()])
    if (Array.isArray(stored?.collapsedToolCategories)) {
      collapsedToolCategories.value = [...new Set(stored.collapsedToolCategories.filter(id =>
        categoryDefinitions.some(category => category.id === id) || id === 'other',
      ))]
    }
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
  <div class="page tools-page audio-tools-page" :class="{ 'audio-tools-page--active': route.name === 'tools' }">
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
    <AppSelect :aria-label="t('tools.toolNavigation')"
      :value="mobileSelectedValue"
      :options="mobileToolOptions"
      class="audio-tools-mobile-select"
      data-guide="audio-tools"
      @update:value="selectMobileTool"
    />

    <div class="audio-tools-workspace" data-guide="audio-tools">
      <nav class="audio-tools-nav" :aria-label="t('tools.toolNavigation')">
        <section v-for="category in categories" :key="category.id">
          <button
            type="button"
            class="audio-tools-group-toggle"
            :class="{ 'has-active-tool': collapsedToolCategories.includes(category.id) && category.entries.some(entry => entry.key === activePluginTool) }"
            :disabled="!restored"
            :aria-expanded="!collapsedToolCategories.includes(category.id)"
            :aria-controls="`audio-tools-category-${category.id}`"
            @click="toggleToolCategory(category.id)"
          >
            <span class="audio-tools-group-title">{{ t(category.titleKey) }}</span>
            <span class="audio-tools-group-count">{{ category.entries.length }}</span>
            <n-icon class="audio-tools-group-chevron" :component="ChevronDownOutline" />
          </button>
          <n-collapse-transition :show="!collapsedToolCategories.includes(category.id)">
            <div :id="`audio-tools-category-${category.id}`" class="audio-tools-group-items" :inert="collapsedToolCategories.includes(category.id)">
              <button
                v-for="entry in category.entries"
                :key="entry.key"
                type="button"
                :class="{ active: activePluginTool === entry.key }"
                @click="activatePluginTool(entry)"
              >
                <n-icon :component="toolIcon(entry)" />
                <span>{{ toolLabel(entry) }}</span>
              </button>
            </div>
          </n-collapse-transition>
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
          <div class="audio-tool-instances">
            <div
              v-for="instance in toolInstances"
              :key="instance.id"
              class="audio-tool-panel"
              :class="{ 'audio-tool-panel--hidden': instance.id !== shownInstanceId }"
              :inert="instance.id !== shownInstanceId"
            >
              <PluginViewHost
                :plugin-name="instance.entry.pluginName"
                :plugins-root="instance.pluginsRoot"
                :view="instance.entry.view"
                @busy-change="value => onPluginBusy(instance.id, value)"
              />
            </div>
          </div>
          <Transition name="tool-panel" mode="out-in">
            <div v-if="!shownInstanceId" :key="activePluginHost ? `${activePluginTool}/${activePluginHost.entry.availability}` : 'empty'" class="audio-tool-panel">
              <section v-if="activePluginHost" class="plugin-tool-unavailable">
                <h2>{{ toolLabel(activePluginHost.entry) }}</h2>
                <p>{{ t(activePluginHost.entry.availability === 'not_prepared' ? 'plugins.prepareRuntimeHint' : 'plugins.unavailableRuntimeHint') }}</p>
                <p v-if="activePluginHost.entry.error">{{ activePluginHost.entry.error }}</p>
                <template v-if="activePluginHost.entry.availability === 'not_prepared' && activePluginHost.entry.availableBindings?.length">
                  <AppSelect :aria-label="t('plugins.chooseSourceVersion')" v-if="activePluginHost.entry.sourceSelectionRequired" v-model:value="selectedPreparationSource"
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
          </Transition>
        </div>
      </main>
    </div>
    </template>
  </div>
</template>

<style src="@/features/plugin-marketplace/tools.css"></style>
