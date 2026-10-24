<script setup lang="ts">
import { computed, onMounted, provide, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { useBeginnerGuideStore } from '@/stores/beginnerGuide'
import { audioTools } from '@/features/audio-tools/registry'
import { isAudioToolAvailable } from '@/features/audio-tools/availability'
import { audioToolRuntimeKey, createAudioToolRuntime } from '@/features/audio-tools/runtime'
import { loadAudioToolsState, updateAudioToolsState } from '@/features/audio-tools/state'
import type { AudioToolCategory, AudioToolKey } from '@/features/audio-tools/types'
import { usePluginMarketplace } from '@/features/plugin-marketplace/composable'
import PluginViewHost from '@/features/plugin-marketplace/PluginViewHost.vue'
import { useAppStore } from '@/stores/app'
import { detectRuntimePlatform } from '@/utils/runtime'
import { recordFeatureVisit } from '@/utils/logger'
defineOptions({ name: 'ToolsView' })
const { t } = useI18n()
const app = useAppStore()
const route = useRoute()
const router = useRouter()
const guide = useBeginnerGuideStore()
import { invoke } from '@tauri-apps/api/core'
import { ExtensionPuzzleOutline } from '@vicons/ionicons5'
const pluginMarket = usePluginMarketplace()
// Plugin-contributed tool pages (protocol v1 views with placement "tool").
const pluginToolViews = computed(() =>
  (pluginMarket.snapshot.value?.installed || [])
    .filter(plugin => plugin.managed && plugin.loaded)
    .flatMap(plugin =>
      (plugin.contributions?.views || [])
        .filter(view => view.placements.includes('tool'))
        .map(view => ({ pluginName: plugin.name, view })),
    ),
)
const activePluginTool = ref('')
const activePluginToolView = computed(() =>
  pluginToolViews.value.find(entry => `${entry.pluginName}/${entry.view.id}` === activePluginTool.value) || null,
)
watch(pluginMarket.snapshot, async snapshot => {
  if (snapshot?.pluginsDir) {
    await invoke('set_plugin_view_root', { root: snapshot.pluginsDir }).catch(() => undefined)
  }
  if (activePluginTool.value && !pluginToolViews.value.some(entry => `${entry.pluginName}/${entry.view.id}` === activePluginTool.value)) {
    activePluginTool.value = ''
  }
}, { immediate: true })
const runtime = createAudioToolRuntime()
provide(audioToolRuntimeKey, runtime)
const activeTool = ref<AudioToolKey>('convert')
watch(() => route.query.tool, (tool) => {
  if (audioTools.some(definition => definition.id === tool)) activeTool.value = tool as AudioToolKey
}, { immediate: true })
watch(() => route.query.pluginView, value => {
  if (typeof value === 'string' && pluginToolViews.value.some(entry => `${entry.pluginName}/${entry.view.id}` === value)) {
    activePluginTool.value = value
  }
}, { immediate: true })
watch(pluginToolViews, views => {
  const query = route.query.pluginView
  if (typeof query === 'string' && views.some(entry => `${entry.pluginName}/${entry.view.id}` === query)) {
    activePluginTool.value = query
  }
}, { immediate: true })
const isMacOS = computed(() => detectRuntimePlatform(app.runtimeInfo).isMac)
const visibleAudioTools = computed(() => audioTools.filter(tool => isAudioToolAvailable(tool, isMacOS.value)))
const activeDefinition = computed(() => visibleAudioTools.value.find(tool => tool.id === activeTool.value) || visibleAudioTools.value[0] || audioTools[0])
const mobileToolOptions = computed(() => visibleAudioTools.value
  .map(tool => ({ label: t(tool.titleKey), value: tool.id })))
const categories: Array<{ id: AudioToolCategory; titleKey: string }> = [
  { id: 'convert', titleKey: 'tools.categoryConvert' },
  { id: 'analyze', titleKey: 'tools.categoryAnalyze' },
  { id: 'recognize', titleKey: 'tools.categoryRecognize' },
  { id: 'edit', titleKey: 'tools.categoryEdit' },
]
const toolsByCategory = (category: AudioToolCategory) => visibleAudioTools.value.filter(tool => tool.category === category)
let restored = false
let lastVisitedTool: AudioToolKey | undefined
function recordActiveToolVisit() {
  const tool = activeDefinition.value.id
  if (!restored || tool === lastVisitedTool) return
  lastVisitedTool = tool
  recordFeatureVisit('tools', tool)
}
function applyRequestedTool() {
  const requested = route.query.tool
  if (typeof requested === 'string' && visibleAudioTools.value.some(tool => tool.id === requested)) {
    if (guide.phase === 'running' && guide.currentStep.tool !== requested) guide.pause()
    activeTool.value = requested as AudioToolKey
  }
}
function selectTool(tool: AudioToolKey) {
  if (activeTool.value === tool) return
  if (guide.phase === 'running' && guide.currentStep.tool !== tool) guide.pause()
  activeTool.value = tool
  if (typeof route.query.tool === 'string') void router.replace({ query: { ...route.query, tool } })
}
watch([() => route.query.tool, visibleAudioTools], applyRequestedTool, { immediate: true })
onMounted(async () => {
  const stored = await loadAudioToolsState()
  if (stored?.activeTool && visibleAudioTools.value.some(tool => tool.id === stored.activeTool)) activeTool.value = stored.activeTool
  applyRequestedTool()
  restored = true
  recordActiveToolVisit()
  await runtime.start()
})
watch(visibleAudioTools, tools => { if (!tools.some(tool => tool.id === activeTool.value) && tools[0]) activeTool.value = tools[0].id }, { immediate: true })
watch(activeTool, value => { if (restored) { recordActiveToolVisit(); void updateAudioToolsState({ activeTool: value }) } })
</script>
<template><div class="page tools-page audio-tools-page"><div class="page-header-compact"><div><h1>{{ t('tools.title') }}</h1><p>{{ t('tools.subtitle') }}</p></div></div>
  <n-select :value="activeTool" @update:value="selectTool" class="audio-tools-mobile-select" data-guide="audio-tools" :options="mobileToolOptions" />
  <div class="audio-tools-workspace"><nav class="audio-tools-nav" :aria-label="t('tools.toolNavigation')"><section v-for="category in categories" :key="category.id"><strong>{{ t(category.titleKey) }}</strong><button v-for="tool in toolsByCategory(category.id)" :key="tool.id" type="button" :class="{ active: !activePluginTool && activeTool === tool.id }" @click="activeTool = tool.id"><n-icon :component="tool.icon" /><span>{{ t(tool.titleKey) }}</span></button></section><section v-if="pluginToolViews.length"><strong>{{ t('tools.categoryPlugins') }}</strong><button v-for="entry in pluginToolViews" :key="`${entry.pluginName}/${entry.view.id}`" type="button" :class="{ active: activePluginTool === `${entry.pluginName}/${entry.view.id}` }" @click="activePluginTool = `${entry.pluginName}/${entry.view.id}`"><n-icon :component="ExtensionPuzzleOutline" /><span>{{ entry.view.title || entry.view.id }}</span></button></section></nav>
    <main class="audio-tools-content">
      <PluginViewHost v-if="activePluginToolView" :key="activePluginTool" :plugin-name="activePluginToolView.pluginName" :view="activePluginToolView.view" />
      <KeepAlive v-if="!activePluginToolView"><component :is="activeDefinition.component" :key="activeDefinition.id" /></KeepAlive>
    </main>
  </div>
</div></template>
<style src="@/features/audio-tools/styles.css"></style>
