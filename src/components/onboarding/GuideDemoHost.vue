<script setup lang="ts">
import { onBeforeUnmount, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { normalizeBeginnerGuideProgress, type BeginnerGuideProgress } from '@/features/onboarding/guide'
import { createLogger } from '@/utils/logger'

const props = defineProps<{ locale: string; themeMode: string; themeAccent: string; platform: string; progress: BeginnerGuideProgress }>()
const emit = defineEmits<{ exit: [status: 'skipped' | 'completed']; progress: [progress: BeginnerGuideProgress] }>()
const { t } = useI18n()
const log = createLogger('ui.onboarding')
const frame = ref<HTMLIFrameElement | null>(null)
const frameKey = ref(0)
const state = ref<'loading' | 'ready' | 'slow' | 'error'>('loading')
function frameSource() {
  const url = new URL(window.location.href)
  url.search = new URLSearchParams({ 'tutorial-frame': '1', locale: props.locale, theme: props.themeMode, accent: props.themeAccent,
    platform: props.platform, progress: JSON.stringify(props.progress) }).toString()
  url.hash = '/'
  return url.href
}
const source = ref(frameSource())
let timeout: ReturnType<typeof setTimeout> | undefined
function waitForReady() {
  clearTimeout(timeout)
  timeout = setTimeout(() => { if (state.value === 'loading') state.value = 'slow' }, 45000)
}
function disposeFrame() {
  try { (frame.value?.contentWindow as any)?.__pymssTutorialDispose?.() } catch { /* The frame can already be unloading. */ }
}
function reload() {
  disposeFrame()
  state.value = 'loading'
  source.value = frameSource()
  frameKey.value += 1
  waitForReady()
}
function message(event: MessageEvent) {
  if (event.source !== frame.value?.contentWindow || event.origin !== window.location.origin) return
  const data = event.data
  if (data?.type === 'pymss-tutorial-ready') { state.value = 'ready'; clearTimeout(timeout) }
  if (data?.type === 'pymss-tutorial-error') {
    state.value = 'error'
    clearTimeout(timeout)
    log.error('Failed to load guided tour', typeof data.message === 'string' ? data.message.slice(0, 1000) : '')
  }
  if (data?.type === 'pymss-tutorial-exit' && ['skipped', 'completed'].includes(data.status)) emit('exit', data.status)
  if (data?.type === 'pymss-tutorial-progress') emit('progress', normalizeBeginnerGuideProgress(data.progress))
}
// The workspace behind the demo is inert, so focus must enter the demo and come back afterwards.
const returnFocus = document.activeElement
onMounted(() => {
  window.addEventListener('message', message)
  waitForReady()
  frame.value?.focus()
})
onBeforeUnmount(() => {
  clearTimeout(timeout)
  window.removeEventListener('message', message)
  disposeFrame()
})
// Unmounted hooks run after the workspace leaves its inert state, so it can take focus again.
onUnmounted(() => {
  const target = returnFocus instanceof HTMLElement && returnFocus.isConnected ? returnFocus : document.getElementById('main-content')
  target?.focus({ preventScroll: true })
})
</script>

<template>
  <section class="tutorial-data-host" :class="{ 'tutorial-data-host--mac': platform === 'darwin' }" role="dialog" aria-modal="true" :aria-label="t('demo.title')">
    <iframe :key="frameKey" ref="frame" :src="source" :title="t('demo.title')" sandbox="allow-scripts allow-same-origin" @error="state = 'error'" />
    <div v-if="state !== 'ready'" class="tutorial-data-host__loading" role="status" aria-live="polite">
      <n-spin v-if="state !== 'error'" size="small" />
      <p>{{ t(state === 'error' ? 'demo.loadFailed' : state === 'slow' ? 'demo.loadSlow' : 'demo.loading') }}</p>
      <n-button v-if="state === 'slow' || state === 'error'" secondary @click="reload">{{ t('demo.retry') }}</n-button>
    </div>
    <span class="tutorial-data-host__badge">{{ t('demo.title') }}</span>
    <button type="button" class="guide-demo-exit" @click="emit('exit', 'skipped')">{{ t('demo.exit') }}</button>
  </section>
</template>

<style scoped>
.tutorial-data-host { position: fixed; inset: 0; z-index: 4000; background: var(--surface); }
.tutorial-data-host iframe { display: block; width: 100%; height: 100%; border: 0; }
.tutorial-data-host__loading { position: absolute; inset: 40px 0 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 16px; background: var(--surface); color: var(--on-surface-muted); font-size: 13px; }
.tutorial-data-host__loading p { margin: 0; max-width: 360px; padding: 0 20px; text-align: center; }
.tutorial-data-host__badge { position: fixed; top: 10px; left: 240px; font-size: 11px; color: var(--primary-strong); pointer-events: none; }
.guide-demo-exit { position: fixed; top: 5px; right: 150px; min-height: 30px; border: 1px solid var(--outline-strong); border-radius: 6px; padding: 4px 12px; background: var(--surface-2); color: var(--on-surface); font: inherit; font-size: 12px; cursor: pointer; }
.guide-demo-exit:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.tutorial-data-host--mac .guide-demo-exit { right: 16px; }
</style>
