<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, useId, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { useBeginnerGuideStore } from '@/stores/beginnerGuide'
import { useSettingsStore } from '@/stores/settings'
import { useAppStore } from '@/stores/app'
import { BEGINNER_GUIDE_STEPS, BEGINNER_GUIDE_CHAPTERS } from '@/features/onboarding/guide'
import { detectRuntimePlatform } from '@/utils/runtime'
import GuideDemoHost from '@/components/onboarding/GuideDemoHost.vue'
import { isTutorialFrame, tutorialAdapter } from '@/features/onboarding/demoContext'
import { lockTutorialInteraction } from '@/features/onboarding/demoInteraction'
import type { DemoRuntime } from '@/features/onboarding/demoRuntime'
import { useTaskStore } from '@/stores/task'

const props = defineProps<{ enabled: boolean }>()
const guide = useBeginnerGuideStore()
const settings = useSettingsStore()
const app = useAppStore()
const inFrame = isTutorialFrame()
const route = useRoute()
const router = useRouter()
const frameTask = inFrame ? useTaskStore() : null
const { t } = useI18n()
const message = useMessage()
const titleId = useId()
const maskId = useId()
const card = ref<HTMLElement | null>(null)
const navigating = computed({ get: () => guide.navigating === true, set: value => { guide.navigating = value } })
const positionedStep = ref<number | null>(null)
const targetRect = ref<{ top: number; left: number; width: number; height: number } | null>(null)
const viewportSize = ref({ width: window.innerWidth, height: window.innerHeight })
const cardPosition = ref({ top: '64px', right: '24px' } as Record<string, string>)
const current = computed(() => guide.currentStep)
const isFinalStep = computed(() => guide.step === BEGINNER_GUIDE_STEPS.length - 1)
const running = computed(() => inFrame && props.enabled && guide.phase === 'running')
const isParameterStep = computed(() => Boolean(current.value.parameters))
const showCoach = computed(() => running.value && !isParameterStep.value)
const coachReady = computed(() => showCoach.value && !navigating.value
  && positionedStep.value === guide.step)
const progress = computed(() => guide.progress || `${guide.step + 1} / ${BEGINNER_GUIDE_STEPS.length}`)
const description = computed(() => {
  if (current.value.anchor === 'model-management' && detectRuntimePlatform(app.runtimeInfo).isMac) return t('beginnerGuide.managementMacDescription')
  if (current.value.anchor === 'runtime-overview' && detectRuntimePlatform(app.runtimeInfo).isMac) return t('beginnerGuide.runtimeMacDescription')
  if (current.value.id === 'device' && detectRuntimePlatform(app.runtimeInfo).isMac) return t('beginnerGuide.deviceMacDescription')
  return t(current.value.description)
})
let disposed = false
let frame = 0
let observer: MutationObserver | null = null
let resizeObserver: ResizeObserver | null = null
let observedTarget: HTMLElement | null = null
let handledRequest = -1
let focusedRequest = -1
let fittedRequest = -1

watch([() => props.enabled, () => settings.initialized], ([enabled, initialized]) => {
  if (enabled && initialized) guide.offer()
}, { immediate: true })
watch(() => detectRuntimePlatform(app.runtimeInfo).isMac, value => guide.setPlatform(value), { immediate: true })

let preparing = false
let unlock: (() => void) | undefined
function targetForStep(): HTMLElement | null {
  if (guide.parameterStep) return document.querySelector(`[data-parameter-guide="${guide.currentParameter?.id}"]`)
  if (current.value.id === 'modelManage') return document.querySelector('.n-drawer .storage-summary') || document.querySelector('[data-guide="model-management"], [data-guide="model-batch"]')
  if (current.value.id === 'pluginsOverview') return document.querySelector('.plugins-runtime-details') || document.querySelector('[data-guide="plugin-library"]')
  if (current.value.id === 'editorMix') return document.querySelector('.tutorial-native-window .track-row__head')
  if (current.value.id === 'editorClips') return document.querySelector('.tutorial-native-window .track-row__lane')
  if (current.value.id === 'editorExport') return document.querySelector('.editor-export-modal .export-dialog__footer') || document.querySelector('.transport-export')
  if (current.value.id === 'workflowEditor') return document.querySelector('.tutorial-native-window .simple-node--step')
    || document.querySelector('.tutorial-native-window .simple-node-editor__topbar, .tutorial-native-window .litegraph-editor .toolbar')
  if (current.value.id === 'workflowOutput') return document.querySelector('.simple-workflow-settings-modal, .advanced-output-settings-modal')
    || document.querySelector('.tutorial-native-window .simple-node-editor__actions, .tutorial-native-window .litegraph-editor .toolbar')
  return document.querySelector(`[data-guide="${current.value.anchor}"]`)
}
function interactionTarget() {
  if (preparing) return document.body
  if (current.value.id === 'modelBatch') return document.querySelector<HTMLElement>('.page--models')
  if (current.value.id === 'resultManage') return document.querySelector<HTMLElement>('.results-page')
  return targetForStep()
}
async function navigateToStep() {
  if (!inFrame) return
  const request = guide.navigationRequest
  navigating.value = true
  preparing = true
  try {
    const runtime = tutorialAdapter() as DemoRuntime
    const id = current.value.id
    document.querySelector<HTMLElement>('.model-batch-modal .n-card-header__close')?.click()
    document.querySelector<HTMLElement>('.n-dialog__close')?.click()
    if (id !== 'modelManage') document.querySelector<HTMLElement>('.n-drawer-header__close')?.click()
    if (id !== 'modelBatch') document.querySelector<HTMLElement>('[data-guide-action="batch-exit"]')?.click()
    if (id !== 'resultManage') document.querySelector<HTMLElement>('[data-guide-action="result-exit"]')?.click()
    if (['run','queue'].includes(id) && frameTask && !frameTask.inputFiles.length) frameTask.inputFiles = [runtime.input]
    const popup = id.startsWith('editor') ? (route.path === '/editor' ? route.fullPath : '/editor?projectId=edit_sample-separation')
      : ['workflowEditor','workflowOutput'].includes(id) ? (route.path.startsWith('/workflow-') ? route.fullPath : '/workflow-simple-editor?workflowId=sample-workflow') : null
    const destination = popup || (current.value.section ? { path: current.value.route, query: { section: current.value.section } } : current.value.route)
    if (route.fullPath !== router.resolve(destination).fullPath) await router.push(destination)
    await nextTick()
    document.querySelector<HTMLElement>('.simple-inference-modal .n-card-header__close')?.click()
    if (id === 'workflowEditor') {
      const fit = [...document.querySelectorAll<HTMLButtonElement>('.simple-node-editor__actions button')].find(button => button.textContent?.trim() === t('workflows.fitView'))
      fit?.click()
    }
    if (id === 'workflowOutput') {
      if (route.path === '/workflow-node-editor') {
        const output = [...document.querySelectorAll<HTMLButtonElement>('.litegraph-editor .toolbar button')].find(button => button.textContent?.trim() === t('separate.output'))
        output?.click()
      } else document.querySelector<HTMLElement>(`.simple-node-editor__actions button[aria-label="${t('workflows.settingsAction')}"]`)?.click()
    }
    if (id === 'editorExport') document.querySelector<HTMLElement>('.transport-export')?.click()
    await nextTick()
  } catch (error) {
    if (!disposed) message.error(error instanceof Error ? error.message : String(error))
  } finally {
    preparing = false
    if (!disposed && request === guide.navigationRequest) navigating.value = false
    schedulePosition()
  }
}

watch([() => guide.navigationRequest, () => guide.phase, () => props.enabled], ([request, phase, enabled]) => {
  if (!inFrame || !enabled || phase !== 'running' || request === handledRequest) return
  handledRequest = request
  void navigateToStep()
}, { immediate: true })
function resume() {
  if (guide.phase === 'paused') guide.start()
}

function position() {
  frame = 0
  if (!showCoach.value || !card.value) return
  viewportSize.value = { width: window.innerWidth, height: window.innerHeight }
  if (navigating.value) return
  const contentTop = document.querySelector('.title-bar')?.getBoundingClientRect().bottom || 0
  let candidates = current.value.anchor
    ? Array.from(document.querySelectorAll<HTMLElement>(`[data-guide="${current.value.anchor}"]`) || []) : []
  if (current.value.anchor === 'model-library') {
    candidates = [...candidates, ...(document.querySelectorAll<HTMLElement>('[data-guide="model-library-fallback"]') || [])]
  }
  if (current.value.fallback) {
    candidates = [...candidates, ...(document.querySelectorAll<HTMLElement>(`[data-guide="${current.value.fallback}"]`) || [])]
  }
  if (current.value.tool) {
    candidates = candidates.filter(element => element.closest('[data-tool]')?.getAttribute('data-tool') === current.value.tool)
  }
  const anchor = targetForStep()
  if (anchor) candidates.unshift(anchor)
  if (current.value.id === 'workflowEditor' && candidates[0] && fittedRequest !== guide.navigationRequest) {
    const fit = [...document.querySelectorAll<HTMLButtonElement>('.simple-node-editor__actions button')].find(button => button.textContent?.trim() === t('workflows.fitView'))
    if (fit) { fittedRequest = guide.navigationRequest; fit.click(); void nextTick(schedulePosition); return }
  }
  const available = candidates.find(element => element.getClientRects().length > 0)
  if (available && focusedRequest !== guide.navigationRequest) available.scrollIntoView({ block: 'nearest', behavior: 'instant' })
  const target = candidates.find(element => {
    const bounds = element.getBoundingClientRect()
    return element.getClientRects().length > 0 && bounds.bottom > contentTop && bounds.top < window.innerHeight && bounds.right > 0 && bounds.left < window.innerWidth
  }) || null
  if (target !== observedTarget) {
    if (observedTarget) resizeObserver?.unobserve(observedTarget)
    observedTarget = target
    if (target) resizeObserver?.observe(target)
  }
  const viewport = { width: window.innerWidth, height: window.innerHeight }
  const margin = viewport.width <= 900 ? 12 : 16
  const gap = viewport.width <= 900 ? 12 : 18
  const size = card.value.getBoundingClientRect()
  const minimumTop = contentTop + 12
  let rect = target?.getBoundingClientRect()
  if (target && rect && rect.right + gap + size.width > viewport.width - margin
    && rect.left - gap - size.width < margin
    && rect.bottom + gap + size.height > viewport.height - margin
    && rect.top - gap - size.height < minimumTop) {
    target.scrollIntoView({ block: 'start', behavior: 'instant' })
    rect = target.getBoundingClientRect()
  }
  targetRect.value = rect && rect.bottom > contentTop && rect.top < viewport.height && rect.right > 0 && rect.left < viewport.width
    ? { left: Math.max(4, rect.left - 4), top: Math.max(contentTop + 4, rect.top - 4), width: Math.min(viewport.width - 8, rect.right + 4) - Math.max(4, rect.left - 4), height: Math.min(viewport.height - 4, rect.bottom + 4) - Math.max(contentTop + 4, rect.top - 4) }
    : null
  let left = viewport.width - size.width - margin, top = 64
  if (rect && targetRect.value) {
    top = rect.top
    if (rect.right + gap + size.width <= viewport.width - margin) left = rect.right + gap
    else if (rect.left - gap - size.width >= margin) left = rect.left - gap - size.width
    else {
      left = rect.right - size.width
      top = rect.bottom + gap + size.height <= viewport.height - margin ? rect.bottom + gap : rect.top - size.height - gap
    }
  }
  cardPosition.value = {
    left: `${Math.max(margin, Math.min(left, viewport.width - size.width - margin))}px`,
    top: `${Math.max(minimumTop, Math.min(top, viewport.height - size.height - margin))}px`,
  }
  positionedStep.value = guide.step
  if (focusedRequest !== guide.navigationRequest) {
    focusedRequest = guide.navigationRequest
    void nextTick(() => {
      if (coachReady.value) card.value?.querySelector<HTMLElement>('.beginner-guide-coach__actions button:last-child')?.focus({ preventScroll: true })
    })
  }
}
function schedulePosition() { if (!frame && !disposed) frame = requestAnimationFrame(position) }
function stopTracking() {
  observer?.disconnect(); observer = null
  resizeObserver?.disconnect(); resizeObserver = null; observedTarget = null
  window.removeEventListener('resize', schedulePosition)
  window.removeEventListener('scroll', schedulePosition, true)
  if (frame) cancelAnimationFrame(frame)
  frame = 0; targetRect.value = null; positionedStep.value = null
}
watch(showCoach, async show => {
  stopTracking()
  if (!show) return
  await nextTick()
  if (!showCoach.value || disposed) return
  resizeObserver = new ResizeObserver(schedulePosition)
  if (card.value) resizeObserver.observe(card.value)
  const content = document.body
  observer = new MutationObserver(records => {
    if (records.some(record => !(record.target instanceof Element)
      || !record.target.closest('.beginner-guide-layer, .beginner-guide-coach, .beginner-guide-highlight'))) schedulePosition()
  })
  if (content) observer.observe(content, { childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'class'] })
  window.addEventListener('resize', schedulePosition)
  window.addEventListener('scroll', schedulePosition, true)
  schedulePosition()
}, { immediate: true })
watch([() => guide.step, description], schedulePosition)
watch(() => route.fullPath, schedulePosition)
onBeforeUnmount(() => { disposed = true; stopTracking(); unlock?.() })
watch(running, active => {
  unlock?.()
  unlock = active ? lockTutorialInteraction(interactionTarget, () => guide.parameterStep) : undefined
}, { immediate: true })

function viewResults() {
  guide.goTo('results')
}

function finish() { guide.end('completed') }
</script>

<template>
  <n-modal :show="enabled && guide.phase === 'invitation'" :mask-closable="false" @update:show="(show: boolean) => { if (!show) guide.end('skipped') }">
    <section class="beginner-guide-invite" role="dialog" aria-modal="true" :aria-labelledby="titleId">
      <span class="beginner-guide__eyebrow">{{ t('beginnerGuide.label') }}</span>
      <h2 :id="titleId">{{ t(settings.beginnerGuideProgress.status === 'active' ? 'beginnerGuide.resumeTitle' : 'beginnerGuide.inviteTitle') }}</h2>
      <p>{{ t('beginnerGuide.inviteDescription') }}</p>
      <ol class="beginner-guide-invite__outline"><li v-for="chapter in Object.values(BEGINNER_GUIDE_CHAPTERS)" :key="chapter">{{ t(chapter) }}</li></ol>
      <small>{{ t('beginnerGuide.durationNote') }}</small>
      <div class="beginner-guide-invite__actions"><n-button text @click="guide.end('skipped')">{{ t('beginnerGuide.skip') }}</n-button><n-button type="primary" @click="guide.start()">{{ t('beginnerGuide.start') }}</n-button></div>
    </section>
  </n-modal>
  <Teleport to="body">
    <GuideDemoHost v-if="!inFrame && guide.demoActive" :locale="String($i18n.locale)" :theme-mode="settings.themeMode" :theme-accent="settings.themeAccent" :platform="app.runtimeInfo?.platform || (detectRuntimePlatform(app.runtimeInfo).isMac ? 'darwin' : 'win32')" :progress="settings.beginnerGuideProgress" @progress="settings.beginnerGuideProgress = $event" @exit="guide.end($event)" />
    <div v-if="running" class="beginner-guide-layer" :class="{ 'beginner-guide-layer--coach': showCoach }">
      <svg class="beginner-guide-mask" aria-hidden="true" :viewBox="`0 0 ${viewportSize.width} ${viewportSize.height}`" preserveAspectRatio="none">
        <defs><mask :id="maskId" x="0" y="0" maskUnits="userSpaceOnUse" :width="viewportSize.width" :height="viewportSize.height"><rect :width="viewportSize.width" :height="viewportSize.height" fill="white" /><rect v-if="targetRect && showCoach" :x="targetRect.left" :y="targetRect.top" :width="targetRect.width" :height="targetRect.height" rx="9" fill="black" /></mask></defs>
        <rect :width="viewportSize.width" :height="viewportSize.height" fill="black" fill-opacity=".28" :mask="`url(#${maskId})`" />
      </svg>
      <div v-if="showCoach && targetRect" class="beginner-guide-highlight" aria-hidden="true" :style="{ left: targetRect.left + 'px', top: targetRect.top + 'px', width: targetRect.width + 'px', height: targetRect.height + 'px' }" />
      <button v-if="showCoach && !coachReady" type="button" class="beginner-guide-preparing-skip" @click="guide.end('skipped')">{{ t('beginnerGuide.skip') }}</button>
      <section v-if="showCoach" ref="card" class="beginner-guide-coach guide-coach" :style="[cardPosition, { visibility: coachReady ? 'visible' : 'hidden' }]" :inert="!coachReady || undefined" :aria-hidden="!coachReady" role="region" :aria-label="t('beginnerGuide.label')">
        <div class="guide-coach__head"><span>{{ t(guide.chapterTitle) }} <span class="guide-coach__count">{{ progress }}</span></span><button type="button" class="guide-coach__skip" @click="guide.end('skipped')">{{ t('beginnerGuide.skip') }}</button></div>
        <h3>{{ t(current.title) }}</h3>
        <p aria-live="polite">{{ description }}</p>
        <n-button v-if="current.id === 'run'" text size="small" class="beginner-guide-coach__results" @click="viewResults">{{ t('beginnerGuide.viewResults') }}</n-button>
        <div class="guide-coach__footer"><n-button v-if="guide.hasNextChapter" text size="tiny" :disabled="navigating" @click="guide.skipChapter()">{{ t('beginnerGuide.skipChapter') }}</n-button><div class="beginner-guide-coach__actions guide-coach__actions"><n-button v-if="guide.step > 0" text size="small" :disabled="navigating" @click="guide.move(-1)">{{ t('common.back') }}</n-button><n-button type="primary" size="small" :disabled="navigating" @click="isFinalStep ? finish() : guide.move(1)">{{ t(isFinalStep ? 'beginnerGuide.finish' : 'common.next') }}</n-button></div></div>
      </section>
    </div>
    <aside v-if="inFrame && enabled && guide.phase === 'paused'" class="beginner-guide-paused" :aria-label="t('beginnerGuide.label')">
      <p>{{ t(guide.pauseReason === 'runtime' ? 'beginnerGuide.runtimePaused' : guide.pauseReason === 'mode' ? 'beginnerGuide.modePaused' : 'beginnerGuide.paused') }}</p>
      <div><n-button text size="small" @click="guide.end('skipped')">{{ t('beginnerGuide.skip') }}</n-button><n-button secondary size="small" @click="resume">{{ t('beginnerGuide.resume') }}</n-button></div>
    </aside>
  </Teleport>
</template>

<style scoped>
.beginner-guide-invite { width: min(430px, calc(100vw - 32px)); padding: 32px; border: 1px solid var(--outline); border-radius: 14px; background: var(--surface-2); color: var(--on-surface); }
.beginner-guide__eyebrow { color: var(--on-surface-muted); font-size: 12px; }
.beginner-guide-invite h2 { margin: 17px 0 10px; font-size: 23px; line-height: 1.4; font-weight: 600; }
.beginner-guide-invite p { margin: 0; color: var(--on-surface-muted); font-size: 14px; line-height: 1.8; }
.beginner-guide-invite__outline { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px 20px; margin: 24px 0 18px; padding-left: 20px; font-size: 13px; }
.beginner-guide-invite__outline li { padding-left: 6px; }
.beginner-guide-invite__outline li::marker { color: var(--primary); font-variant-numeric: tabular-nums; }
.beginner-guide-invite small { color: var(--on-surface-muted); font-size: 11px; }
.beginner-guide-invite__actions { display: flex; justify-content: flex-end; align-items: center; gap: 24px; margin-top: 30px; }
.beginner-guide-layer { position: fixed; inset: 0; pointer-events: none; z-index: 1000; }
.beginner-guide-layer--coach { z-index: 4000; }
.beginner-guide-preparing-skip { position: fixed; top: 64px; right: 24px; padding: 6px 12px; pointer-events: auto; border: 1px solid var(--outline-strong); border-radius: 6px; background: var(--surface-2); color: var(--on-surface); font: inherit; font-size: 12px; cursor: pointer; }
.beginner-guide-preparing-skip:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
.beginner-guide-mask { position: absolute; inset: 0; width: 100%; height: 100%; }
.beginner-guide-highlight { position: fixed; border: 1px solid var(--primary); border-radius: 9px; }
.beginner-guide-coach { position: fixed; }
@media (max-width: 900px) {
  .beginner-guide-coach.guide-coach { width: min(350px, calc(100vw - 32px)); }
}
.beginner-guide-coach__results { margin-top: 13px; }
.beginner-guide-paused { position: fixed; bottom: 20px; right: 20px; z-index: 1000; width: min(330px, calc(100vw - 40px)); padding: 15px 18px; background: var(--surface-2); border: 1px solid var(--outline-strong); border-radius: 10px; box-shadow: var(--shadow-soft); }
.beginner-guide-paused p { margin: 0 0 13px; color: var(--on-surface-muted); font-size: 13px; line-height: 1.7; }
.beginner-guide-paused > div { display: flex; justify-content: flex-end; gap: 20px; }
</style>

<style scoped src="./beginner-guide-coach.css"></style>
