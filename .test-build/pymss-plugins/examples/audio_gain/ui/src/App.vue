<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { NAlert, NButton, NConfigProvider, NInput, NInputNumber, NModal, NProgress, NSpin, NSwitch, NTag, darkTheme, enUS, zhCN } from 'naive-ui'
import { bounded, connect, host, OPERATION_ID, overlay, sdk, type OperationResult } from './studio'

const { t, locale } = useI18n()
watch(() => host.locale, value => { locale.value = value; document.documentElement.lang = value }, { immediate: true })
const theme = computed(() => host.mode === 'dark' ? darkTheme : null)
const input = ref(''), output = ref(''), gain = ref<number | null>(0), protect = ref(true)
const connecting = ref(true), connectionFailed = ref(false), initialized = ref(false)
const picking = ref(false), running = ref(false), cancelling = ref(false), showLogs = ref(false)
const error = ref(''), actionError = ref(''), settingsFailed = ref(false)
const status = ref<'ready' | 'running' | 'completed' | 'failed' | 'cancelled'>('ready')
const progress = ref(0), elapsed = ref(0)
const result = ref<OperationResult | null>(null)
const report = computed(() => result.value?.outputs.report?.value || {})
const logs = ref<Array<{ id: number; time: number; key?: string; raw?: string }>>([])
const busy = computed(() => running.value || picking.value || !host.ready)
const canRun = computed(() => !busy.value && Boolean(input.value.trim() && output.value.trim()) && typeof gain.value === 'number' && Number.isFinite(gain.value) && gain.value >= -24 && gain.value <= 12)
let activeId: number | null = null, disposed = false, sequence = 0
let saveTimer: ReturnType<typeof setTimeout> | undefined
let elapsedTimer: ReturnType<typeof setInterval> | undefined
let saveQueue = Promise.resolve()

function log(key?: string, raw?: string) {
  if (logs.value.at(-1)?.key === key && logs.value.at(-1)?.raw === raw) return
  logs.value = [...logs.value, { id: ++sequence, time: Date.now(), key, raw }].slice(-100)
}
const errorMessage = (value: unknown) => value instanceof Error ? value.message : String(value)
const filename = (value: string) => value.split(/[\\/]/).at(-1) || value
const peak = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(3) : '—'

function persist() {
  clearTimeout(saveTimer)
  if (!initialized.value || gain.value === null || !Number.isFinite(gain.value)) return
  const data = { gain_db: gain.value, protect_peak: protect.value, output_dir: output.value }
  saveQueue = saveQueue.catch(() => undefined).then(() => bounded(sdk().writeSettings(data))).then(() => { settingsFailed.value = false })
  void saveQueue.catch(() => { settingsFailed.value = true })
}
watch([gain, protect, output], () => {
  if (!initialized.value || disposed) return
  clearTimeout(saveTimer)
  saveTimer = setTimeout(persist, 350)
}, { flush: 'sync' })
watch(showLogs, open => { if (open) overlay(true) })

onMounted(async () => {
  try {
    const connected = await connect()
    if (disposed) return
    const saved = connected.settings || {}
    if (typeof saved.gain_db === 'number' && Number.isFinite(saved.gain_db) && saved.gain_db >= -24 && saved.gain_db <= 12) gain.value = saved.gain_db
    if (typeof saved.protect_peak === 'boolean') protect.value = saved.protect_peak
    output.value = typeof saved.output_dir === 'string' && saved.output_dir.length <= 4096 ? saved.output_dir : connected.outputDir
    initialized.value = true
  } catch (failure) { connectionFailed.value = true; log(undefined, errorMessage(failure)) }
  finally { connecting.value = false }
})

async function pick(kind: 'audio' | 'directory') {
  if (busy.value) return
  picking.value = true; actionError.value = ''
  try {
    const selected = kind === 'audio' ? await sdk().pickAudio() : await sdk().pickDirectory()
    if (!disposed && selected?.path) (kind === 'audio' ? input : output).value = selected.path
  } catch (failure) { actionError.value = errorMessage(failure) }
  finally { picking.value = false }
}

async function run() {
  if (!canRun.value || gain.value === null) return
  const params = { gain_db: gain.value, protect_peak: protect.value }
  const inputs = { audio: { kind: 'file', path: input.value } }
  const outputDir = output.value
  running.value = true; cancelling.value = false; status.value = 'running'
  error.value = ''; actionError.value = ''; result.value = null; progress.value = 0; elapsed.value = 0
  logs.value = []; log('started'); persist()
  const started = Date.now()
  elapsedTimer = setInterval(() => { elapsed.value = Math.floor((Date.now() - started) / 1000) }, 500)
  try {
    const job = sdk().startOperation(OPERATION_ID, params, inputs, update => {
      if (disposed || update.id !== activeId) return
      if (typeof update.fraction === 'number' && Number.isFinite(update.fraction)) progress.value = Math.max(progress.value, Math.min(1, Math.max(0, update.fraction)))
      if (update.message === 'processing' || update.message === 'completed') log(update.message)
      else if (update.message) log(undefined, update.message)
    }, { outputDir })
    activeId = job.id
    const value = await job
    if (disposed) return
    if (value.operation !== OPERATION_ID || value.outputs?.audio?.kind !== 'audio_file' || !value.outputs.audio.path) throw new Error(t('invalidResult'))
    result.value = value; progress.value = 1; status.value = 'completed'; log('completed')
  } catch (failure) {
    if (disposed) return
    if ((failure as { code?: string }).code === 'TASK_CANCELLED') { status.value = 'cancelled'; log('cancelled') }
    else { status.value = 'failed'; error.value = errorMessage(failure); log(undefined, error.value) }
  } finally {
    clearInterval(elapsedTimer)
    elapsed.value = Math.floor((Date.now() - started) / 1000)
    running.value = false; cancelling.value = false; activeId = null
  }
}

async function cancel() {
  if (activeId === null || cancelling.value) return
  cancelling.value = true; actionError.value = ''
  try {
    await bounded(sdk().cancelOperation(activeId))
    // Cancellation acknowledgement does not replace the start request's terminal result.
    if (running.value) log('cancelling')
  } catch (failure) { cancelling.value = false; actionError.value = errorMessage(failure) }
}
async function reveal() {
  const path = result.value?.outputs.audio?.path
  if (!path) return
  actionError.value = ''
  try { await bounded(sdk().revealPath(path)) }
  catch (failure) { actionError.value = errorMessage(failure) }
}
onBeforeUnmount(() => { disposed = true; clearTimeout(saveTimer); clearInterval(elapsedTimer); persist(); overlay(false) })
</script>

<template>
  <n-config-provider :theme="theme" :theme-overrides="host.theme || undefined" :locale="locale === 'zh-CN' ? zhCN : enUS">
    <main class="gain-tool">
      <section class="gain-panel">
        <header class="gain-header"><div><h1>{{ t('title') }}</h1><p>{{ t('description') }}</p></div><n-button size="small" :disabled="!logs.length" @click="showLogs = true">{{ t('logs') }}</n-button></header>
        <n-spin v-if="connecting" size="small"><span>{{ t('connecting') }}</span></n-spin>
        <n-alert v-if="connectionFailed" type="error" :bordered="false">{{ t('connectionFailed') }}</n-alert>
        <n-alert v-if="settingsFailed" type="warning" :bordered="false">{{ t('settingsFailed') }}</n-alert>
        <n-alert v-if="error || actionError" type="error" :bordered="false">{{ error || actionError }}</n-alert>
        <div class="gain-field"><label for="input-audio">{{ t('inputAudio') }}</label><div class="gain-path"><n-input :value="input" readonly :input-props="{ id: 'input-audio' }" :placeholder="t('selectAudio')" /><n-button :disabled="busy" @click="pick('audio')">{{ t('browse') }}</n-button></div></div>
        <div class="gain-field"><label for="output-directory">{{ t('outputDirectory') }}</label><div class="gain-path"><n-input :value="output" readonly :input-props="{ id: 'output-directory' }" /><n-button :disabled="busy" @click="pick('directory')">{{ t('browse') }}</n-button></div></div>
        <div class="gain-options"><div class="gain-field"><label for="gain-db">{{ t('gain') }}</label><n-input-number v-model:value="gain" :min="-24" :max="12" :step="1" :disabled="busy" :input-props="{ id: 'gain-db' }" /></div><label class="gain-switch"><n-switch v-model:value="protect" :disabled="busy" :aria-label="t('protectPeak')" /><span>{{ t('protectPeak') }}</span></label></div>
        <p class="gain-hint">{{ t('gainHint') }}</p>
        <footer class="gain-actions"><span class="gain-status" role="status"><n-tag size="small" :bordered="false" :type="status === 'failed' ? 'error' : status === 'completed' ? 'success' : 'default'">{{ t(`state.${status}`) }}</n-tag><span v-if="running">{{ elapsed }}s</span></span><n-button v-if="running" :disabled="cancelling || activeId === null" :loading="cancelling" :aria-label="t(cancelling ? 'stopping' : 'cancel')" @click="cancel">{{ t(cancelling ? 'stopping' : 'cancel') }}</n-button><n-button v-else type="primary" :disabled="!canRun" @click="run">{{ t('run') }}</n-button></footer>
        <n-progress v-if="running" type="line" :percentage="Math.round(progress * 100)" :show-indicator="true" :aria-label="t('progress')" />
      </section>
      <section v-if="result" class="gain-panel gain-results"><h2>{{ t('results') }}</h2><p :title="result.outputs.audio?.path">{{ filename(result.outputs.audio?.path || '') }}</p><dl><div><dt>{{ t('peakBefore') }}</dt><dd>{{ peak(report.peak_before) }}</dd></div><div><dt>{{ t('peakAfter') }}</dt><dd>{{ peak(report.peak_after) }}</dd></div><div><dt>{{ t('peakLimited') }}</dt><dd>{{ t(report.peak_limited ? 'yes' : 'no') }}</dd></div></dl><n-button secondary @click="reveal">{{ t('openOutput') }}</n-button></section>
    </main>
    <n-modal v-model:show="showLogs" preset="card" :title="t('logTitle')" style="width:min(760px,calc(100vw - 32px));max-height:calc(100dvh - 48px)" @after-leave="overlay(false)">
      <div class="gain-log" role="log"><p v-if="!logs.length">{{ t('noLogs') }}</p><div v-for="entry in logs" :key="entry.id"><time>{{ new Date(entry.time).toLocaleTimeString(locale, { hour12: false }) }}</time><span>{{ entry.key ? t(`event.${entry.key}`) : entry.raw }}</span></div></div>
    </n-modal>
  </n-config-provider>
</template>
