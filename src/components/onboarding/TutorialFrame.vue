<script setup lang="ts">
import { computed, onBeforeUnmount, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { darkTheme } from 'naive-ui'
import { useI18n } from 'vue-i18n'
import TitleBar from '@/components/TitleBar.vue'
import SideNav from '@/components/SideNav.vue'
import BeginnerGuide from '@/components/BeginnerGuide.vue'
import { useSettingsStore } from '@/stores/settings'
import { useBeginnerGuideStore } from '@/stores/beginnerGuide'
import { getThemeOverrides, themeIsDark } from '@/utils/theme'
import type { DemoRuntime } from '@/features/onboarding/demoRuntime'

const props = defineProps<{ runtime: DemoRuntime }>()
const settings = useSettingsStore()
const guide = useBeginnerGuideStore()
const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const theme = computed(() => { void themeIsDark.value; return getThemeOverrides(settings.themeMode, settings.themeAccent) })
const popup = computed(() => ['/editor', '/workflow-simple-editor', '/workflow-node-editor'].includes(route.path))
function closePopup() {
  if (guide.currentStep.chapter === 'results') guide.goTo('ensemble')
  else guide.goTo('workflowOverview')
}
props.runtime.connectWindows(path => {
  void router.push(path).then(() => {
    if (path.startsWith('/editor') && guide.currentStep.id === 'resultEditor') guide.goTo('editorPlayback')
    if (path.startsWith('/workflow-') && ['workflowCreate', 'workflowOverview'].includes(guide.currentStep.id)) guide.goTo('workflowEditor')
  })
}, closePopup)
const stopRouteTracking = router.afterEach((to, from, failure) => {
  if (failure || guide.phase !== 'running' || guide.navigating) return
  if (to.path === '/workflows' && ['/workflow-simple-editor', '/workflow-node-editor'].includes(from.path)
    && ['workflowEditor', 'workflowOutput'].includes(guide.currentStep.id)) guide.goTo('workflowOverview')
  if (to.path === '/results' && from.path === '/editor' && guide.currentStep.id.startsWith('editor')) guide.goTo('resultEditor')
})
watch(() => settings.beginnerGuideProgress, progress => {
  window.parent.postMessage({ type: 'pymss-tutorial-progress', progress: JSON.parse(JSON.stringify(progress)) }, '*')
}, { deep: true })
watch(() => guide.phase, phase => {
  if (phase === 'closed') window.parent.postMessage({ type: 'pymss-tutorial-exit', status: settings.beginnerGuideProgress.status }, '*')
})
onBeforeUnmount(() => { stopRouteTracking(); props.runtime.dispose() })
</script>

<template>
  <n-config-provider :theme="themeIsDark ? darkTheme : null" :theme-overrides="theme">
    <n-notification-provider><n-message-provider><n-dialog-provider>
      <div class="app-shell no-animations" :class="{ 'app-shell--native-titlebar': runtime.runtimeInfo.platform === 'darwin' }">
        <div class="app-backdrop" />
        <TitleBar />
        <div class="app-body">
          <SideNav />
          <main class="app-content">
            <router-view v-slot="{ Component }"><keep-alive include="ToolsView"><component :is="Component" /></keep-alive></router-view>
          </main>
        </div>
        <n-modal :aria-label="t(route.path === '/editor' ? 'demo.editorTitle' : 'demo.workflowEditorTitle')" :show="popup" preset="card" :mask-closable="false" :close-on-esc="false" :title="t(route.path === '/editor' ? 'demo.editorTitle' : 'demo.workflowEditorTitle')"
          class="tutorial-native-window" :z-index="2000" @close="closePopup">
          <router-view name="popup" />
        </n-modal>
        <BeginnerGuide :enabled="true" />
      </div>
    </n-dialog-provider></n-message-provider></n-notification-provider>
  </n-config-provider>
</template>

<style>
.tutorial-native-window { width: calc(100vw - 48px); height: calc(100vh - 96px); max-width: 1440px; }
.tutorial-native-window > .n-card__content, .tutorial-native-window > .n-card-content { min-height: 0; overflow: auto; padding: 0 12px 12px; }
.tutorial-native-window .editor-view, .tutorial-native-window .editor-shell, .tutorial-native-window .simple-editor-page, .tutorial-native-window .workflow-node-editor-page { height: 100%; min-height: 0; }
</style>
