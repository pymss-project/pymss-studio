<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import {
  ArrowDownOutline,
  AlertCircleOutline,
  CheckmarkCircleOutline,
  CloudDownloadOutline,
  CopyOutline,
  PauseCircleOutline,
  PlayOutline,
  TrashOutline,
} from '@vicons/ionicons5'
import type { DownloadLogEntry, DownloadTask } from '@/stores/model'
import { formatBytes, formatSpeedMBps } from '@/utils/format'

const props = defineProps<{
  show: boolean
  task: DownloadTask | null
  modelName: string
}>()

const emit = defineEmits<{
  (e: 'update:show', value: boolean): void
  (e: 'cancel'): void
  (e: 'resume'): void
  (e: 'delete'): void
  (e: 'close'): void
}>()

const { t } = useI18n()
const message = useMessage()

const logContainerRef = ref<HTMLElement | null>(null)
const autoScroll = ref(true)

const statusType = computed(() => {
  if (!props.task) return 'default'
  if (props.task.status === 'error') return 'error'
  if (['preparing', 'downloading'].includes(props.task.status)) return 'info'
  if (['paused', 'cancelled', 'interrupted'].includes(props.task.status)) return 'warning'
  return props.task.status === 'done' ? 'success' : 'default'
})

const statusLabel = computed(() => {
  if (!props.task) return ''
  const map: Record<string, string> = {
    preparing: t('tasks.statusPreparing'),
    downloading: t('models.downloadStatusDownloading'),
    done: t('models.downloaded'),
    error: t('models.downloadStatusError'),
    paused: t('models.downloadStatusPaused'),
    cancelled: t('models.downloadStatusCancelled'),
    interrupted: t('models.downloadInterrupted'),
    idle: t('models.downloadStatusIdle'),
  }
  return map[props.task.status] || props.task.status
})

const progressColor = computed(() => {
  if (!props.task) return 'var(--primary)'
  if (props.task.status === 'error') return 'var(--danger)'
  if (props.task.status === 'done') return 'var(--success)'
  if (['paused', 'cancelled', 'interrupted'].includes(props.task.status)) return 'var(--warning)'
  return 'var(--primary)'
})

const statusIcon = computed(() => {
  if (props.task?.status === 'done') return CheckmarkCircleOutline
  if (props.task?.status === 'error') return AlertCircleOutline
  if (['paused', 'cancelled', 'interrupted'].includes(props.task?.status || '')) return PauseCircleOutline
  return CloudDownloadOutline
})

const displayProgress = computed(() => {
  if (props.task?.status === 'done') return 100
  const progress = props.task?.progress
  return typeof progress === 'number' && Number.isFinite(progress) ? Math.min(100, Math.max(0, Math.round(progress))) : 0
})

const filesText = computed(() => {
  if (!props.task) return ''
  if (props.task.totalFiles > 0) {
    return t('models.fileProgress', {
      completed: props.task.completedFiles,
      total: props.task.totalFiles,
    })
  }
  return ''
})

/** Keep the worker detail separate from the status badge. */
const progressMessage = computed(() => {
  let detail = props.task?.message?.trim()
  switch (detail) {
    case 'Started':
      detail = t('models.downloadStarted')
      break
    case 'resolving_files':
      detail = t('models.downloadPreparing')
      break
    case 'Downloading':
    case 'downloading_files':
    case 'Downloading model files':
      detail = t('models.downloadTransferring')
      break
    case 'verifying':
    case 'Verifying downloaded files':
      detail = t('models.downloadVerifying')
      break
    case 'Done':
      detail = t('models.downloaded')
      break
    case 'Cancelled':
      detail = t('models.downloadStatusCancelled')
      break
    case 'Paused':
      detail = t('models.downloadStatusPaused')
      break
    case 'Failed':
      detail = t('models.downloadStatusError')
      break
  }
  return detail && detail !== statusLabel.value ? detail : ''
})

const speedText = computed(() => {
  if (props.task?.status !== 'downloading') return '—'
  return formatSpeedMBps(props.task?.speedBytesPerSecond) || '—'
})

const bytesText = computed(() => {
  const task = props.task
  if (!task) return '—'
  const downloaded = typeof task.downloadedBytes === 'number' && Number.isFinite(task.downloadedBytes) && task.downloadedBytes >= 0
    ? (task.downloadedBytes === 0 ? '0 B' : formatBytes(task.downloadedBytes)) : '—'
  const total = typeof task.totalBytes === 'number' && Number.isFinite(task.totalBytes) && task.totalBytes > 0
    ? formatBytes(task.totalBytes) : ''
  return total ? `${downloaded} / ${total}` : downloaded
})

const canCancel = computed(() => ['preparing', 'downloading'].includes(props.task?.status || ''))
const canResume = computed(() =>
  props.task && ['paused', 'cancelled', 'error', 'interrupted'].includes(props.task.status),
)
const canDelete = computed(() =>
  props.task && ['paused', 'cancelled', 'error', 'interrupted'].includes(props.task.status),
)

const logs = computed<DownloadLogEntry[]>(() => props.task?.logs || [])

function formatTime(ts: number) {
  const d = new Date(ts)
  const pad = (n: number) => n.toString().padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function levelClass(level: string) {
  return `dl-log--${level}`
}

async function scrollToBottom() {
  if (!autoScroll.value) return
  await nextTick()
  const el = logContainerRef.value
  if (el) el.scrollTop = el.scrollHeight
}

watch(() => logs.value.at(-1), () => void scrollToBottom())
watch(() => props.task?.taskId, () => {
  autoScroll.value = true
  void scrollToBottom()
})
watch(() => props.show, (v) => {
  if (v) void scrollToBottom()
})

function resumeAutoScroll() {
  autoScroll.value = true
  void scrollToBottom()
}

function onScroll(e: Event) {
  const el = e.target as HTMLElement
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  autoScroll.value = atBottom
}

async function copyAllLogs() {
  if (!props.task) return
  const text = props.task.logs
    .map((log) => `[${formatTime(log.ts)}] [${log.level}] ${log.message}`)
    .join('\n')
  if (!text) {
    message.info(t('models.downloadLogsEmpty'))
    return
  }
  try {
    await navigator.clipboard.writeText(text)
    message.success(t('models.downloadLogsCopied'))
  } catch {
    message.error(t('models.downloadLogsCopyFailed'))
  }
}

function handleCancel() { emit('cancel') }
function handleResume() { emit('resume') }
function handleDelete() { emit('delete') }
function handleClose() {
  emit('update:show', false)
  emit('close')
}
</script>

<template>
  <n-modal
    :show="show"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <n-card
      class="download-detail-modal"
      :class="`download-detail-modal--${task?.status || 'idle'}`"
      :bordered="false"
      closable
      role="dialog"
      aria-modal="true"
      :aria-label="t('models.downloadDetailTitle')"
      @close="handleClose"
    >
      <template #header>
        <div class="ddm-header">
          <div class="ddm-header-copy">
            <div class="ddm-title-row">
              <n-icon class="ddm-header-icon" :component="CloudDownloadOutline" aria-hidden="true" />
              <span class="ddm-title">{{ t('models.downloadDetailTitle') }}</span>
            </div>
            <span class="ddm-model" :title="modelName">{{ modelName }}</span>
          </div>
          <n-tag v-if="task" :type="statusType" size="small" :bordered="false" class="ddm-status">
            <template #icon><n-icon :component="statusIcon" /></template>
            {{ statusLabel }}
          </n-tag>
        </div>
      </template>

      <div v-if="task" class="ddm-body">
        <section class="ddm-progress" :aria-label="t('models.downloadProgressLabel')">
          <div class="ddm-progress-info">
            <span class="ddm-progress-msg" :title="progressMessage">{{ progressMessage || t('models.downloadProgressLabel') }}</span>
            <div class="ddm-progress-pct">{{ displayProgress }}<span>%</span></div>
          </div>
          <n-progress
            :percentage="displayProgress"
            :show-indicator="false"
            :height="6"
            :border-radius="3"
            :processing="canCancel"
            type="line"
            :color="progressColor"
            rail-color="var(--outline)"
          />
          <dl class="ddm-transfer-stats">
            <div class="ddm-transfer-stat">
              <dt>{{ t('models.downloadTransferred') }}</dt>
              <dd>{{ bytesText }}</dd>
            </div>
            <div class="ddm-transfer-stat">
              <dt>{{ t('models.downloadSpeed') }}</dt>
              <dd>{{ speedText }}</dd>
            </div>
            <div class="ddm-transfer-stat">
              <dt>{{ t('models.downloadFileCount') }}</dt>
              <dd>{{ filesText || '—' }}</dd>
            </div>
          </dl>
        </section>

        <div v-if="task.errorMessage" class="ddm-error">
          <strong>{{ t('models.downloadErrorLabel') }}</strong>
          <pre class="ddm-error-text">{{ task.errorMessage }}</pre>
        </div>

        <section class="ddm-log-section" :aria-label="t('models.downloadLogs')">
          <div class="ddm-logs-head">
            <div class="ddm-logs-heading">
              <span class="ddm-logs-title">{{ t('models.downloadLogs') }}</span>
              <span v-if="logs.length" class="ddm-logs-count">{{ logs.length }}</span>
            </div>
            <div class="ddm-logs-actions">
              <n-button size="tiny" quaternary :disabled="!logs.length" @click="copyAllLogs">
                <template #icon><n-icon :component="CopyOutline" /></template>
                {{ t('models.downloadCopyLogs') }}
              </n-button>
            </div>
          </div>
          <div
            ref="logContainerRef"
            class="ddm-logs"
            :class="{ 'ddm-logs--empty': !logs.length }"
            @scroll="onScroll"
          >
            <div v-if="!logs.length" class="ddm-logs-empty">
              {{ t('models.downloadLogsEmpty') }}
            </div>
            <div
              v-for="(log, idx) in logs"
              :key="idx"
              :class="['dl-log', levelClass(log.level)]"
            >
              <span class="dl-log-time">{{ formatTime(log.ts) }}</span>
              <span class="dl-log-level">{{ log.level }}</span>
              <span class="dl-log-msg">{{ log.message }}</span>
            </div>
          </div>
          <!-- Scrolling up pauses the follow-along so a line being read does not slide away; this
               is how to get back to it. -->
          <button
            v-if="logs.length && !autoScroll"
            type="button"
            class="ddm-logs-resume"
            @click="resumeAutoScroll"
          >
            <n-icon :component="ArrowDownOutline" />
            {{ t('models.downloadLogsFollow') }}
          </button>
        </section>
      </div>
      <div v-else class="ddm-no-task"><n-empty :description="t('models.downloadTaskEmpty')" /></div>

      <template v-if="canCancel || canResume || canDelete" #footer>
        <div class="ddm-footer">
          <span v-if="canCancel" class="ddm-footer-hint">{{ t('models.downloadBackgroundHint') }}</span>
          <n-button
            v-if="canCancel"
            type="error"
            secondary
            @click="handleCancel"
          >
            {{ t('common.cancel') }}
          </n-button>
          <n-button
            v-if="canDelete"
            type="error"
            secondary
            @click="handleDelete"
          >
            <template #icon><n-icon :component="TrashOutline" /></template>
            {{ t('models.delete') }}
          </n-button>
          <n-button
            v-if="canResume"
            type="primary"
            @click="handleResume"
          >
            <template #icon><n-icon :component="PlayOutline" /></template>
            {{ task?.status === 'interrupted' ? t('models.continueDownload') : t('common.resume') }}
          </n-button>
        </div>
      </template>
    </n-card>
  </n-modal>
</template>

<style scoped>
.download-detail-modal {
  --download-tone: var(--primary);
  width: min(720px, calc(100vw - 48px));
  max-height: min(640px, calc(100dvh - 64px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-radius: 16px;
}

.download-detail-modal--done { --download-tone: var(--success); }
.download-detail-modal--error { --download-tone: var(--danger); }
.download-detail-modal--paused,
.download-detail-modal--cancelled,
.download-detail-modal--interrupted { --download-tone: var(--warning); }

.download-detail-modal :deep(.n-card-header) {
  flex: 0 0 auto;
  padding: 16px 20px 12px;
  border-bottom: 1px solid color-mix(in srgb, var(--outline) 72%, transparent);
}

.download-detail-modal :deep(.n-card-content) {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 0;
  scrollbar-width: thin;
  scrollbar-color: var(--on-surface-muted) transparent;
}

.download-detail-modal :deep(.n-card-footer),
.download-detail-modal :deep(.n-card__footer) {
  flex: 0 0 auto;
  padding: 12px 20px;
  border-top: 1px solid color-mix(in srgb, var(--outline) 72%, transparent);
  background: color-mix(in srgb, var(--surface-1) 96%, transparent);
}

.ddm-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.ddm-title-row { display: flex; align-items: center; gap: 7px; }

.ddm-header-icon {
  color: var(--download-tone);
  font-size: 19px;
}

.ddm-header-copy { display: grid; flex: 1 1 auto; gap: 4px; min-width: 0; }

.ddm-title {
  font-size: 16px;
  font-weight: 600;
}

.ddm-model {
  font-size: 11px;
  color: var(--on-surface-muted);
  font-family: var(--font-mono);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ddm-body {
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px 20px;
  box-sizing: border-box;
}

.ddm-progress {
  display: flex;
  flex-direction: column;
  gap: 10px;
  flex-shrink: 0;
}

.ddm-progress-info {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.ddm-progress-pct {
  flex: 0 0 auto;
  font-size: 28px;
  font-weight: 600;
  line-height: 1.1;
  letter-spacing: -0.035em;
  color: var(--on-surface-heading);
  font-variant-numeric: tabular-nums;
}

.ddm-progress-pct > span { margin-left: 3px; font-size: 14px; color: var(--on-surface-muted); letter-spacing: 0; }
.ddm-status { flex-shrink: 0; }

.ddm-progress-msg {
  flex: 1 1 auto;
  font-size: 12px;
  line-height: 1.5;
  color: var(--on-surface-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.ddm-transfer-stats {
  display: grid;
  grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
  margin: 0;
}
.ddm-transfer-stat { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; min-width: 0; }
.ddm-transfer-stat dt { color: var(--on-surface-muted); font-size: 12px; }
.ddm-transfer-stat dd { margin: 0; font-size: 12px; font-weight: 500; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }

.ddm-error {
  margin-top: 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--danger) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent);
  flex-shrink: 0;
}

.ddm-error strong {
  display: block;
  font-size: 12px;
  color: var(--danger);
  margin-bottom: 4px;
}

.ddm-error-text {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--on-surface);
  white-space: pre-wrap;
  word-break: break-all;
  font-family: var(--font-mono);
  max-height: 100px;
  overflow-y: auto;
}

.ddm-logs-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  flex-shrink: 0;
}

.ddm-log-section { display: flex; flex-direction: column; min-height: 0; border-top: 1px solid var(--outline); padding-top: 12px; }
.ddm-logs-heading { display: flex; align-items: center; gap: 8px; }
.ddm-no-task { padding: 44px 24px; }

.ddm-logs-title {
  font-size: 13px;
  font-weight: 600;
}

.ddm-logs-actions {
  display: flex;
  gap: 6px;
}

.ddm-logs {
  flex: 0 1 auto;
  min-height: 0;
  max-height: 280px;
  position: relative;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 10px;
  border-radius: 8px;
  /* Same console treatment as the separation log, so both read as machine output rather than
     as two different panels that happen to contain log lines. */
  background: #0b1020;
  color: #c9d6ec;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.55;
  scrollbar-width: thin;
  scrollbar-color: #73839c transparent;
}

@supports selector(::-webkit-scrollbar) {
  .ddm-logs { scrollbar-width: auto; scrollbar-color: auto; }
  .download-detail-modal :deep(.n-card-content) { scrollbar-width: auto; scrollbar-color: auto; }
}
.download-detail-modal :deep(.n-card-content::-webkit-scrollbar),
.ddm-error-text::-webkit-scrollbar { width: 7px; }
.download-detail-modal :deep(.n-card-content::-webkit-scrollbar-track),
.ddm-error-text::-webkit-scrollbar-track { background: transparent; }
.download-detail-modal :deep(.n-card-content::-webkit-scrollbar-thumb),
.ddm-error-text::-webkit-scrollbar-thumb { border-radius: 999px; background: color-mix(in srgb, var(--on-surface-muted) 40%, transparent); }
.download-detail-modal :deep(.n-card-content::-webkit-scrollbar-button),
.ddm-error-text::-webkit-scrollbar-button { display: none; width: 0; height: 0; }
.ddm-logs::-webkit-scrollbar { width: 9px; }
.ddm-logs::-webkit-scrollbar-track { background: transparent; }
.ddm-logs::-webkit-scrollbar-thumb { border: 2px solid transparent; border-radius: 999px; background: rgb(148 163 184 / 45%) padding-box; }
.ddm-logs::-webkit-scrollbar-thumb:hover { background: rgb(148 163 184 / 65%) padding-box; }
.ddm-logs::-webkit-scrollbar-button,
.ddm-logs::-webkit-scrollbar-corner { display: none; width: 0; height: 0; }

.ddm-logs-empty {
  color: var(--on-surface-muted);
  text-align: center;
  padding: 4px 0;
  font-size: 12px;
}

.ddm-logs--empty { background: var(--surface-2); border: 1px solid var(--outline); font-family: inherit; }

.ddm-logs-count {
  padding: 0 6px;
  border-radius: 999px;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  color: var(--on-surface-muted);
  background: color-mix(in srgb, var(--surface-3) 70%, transparent);
}

.ddm-logs-resume {
  align-self: center;
  margin-top: 8px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border: 0;
  border-radius: 999px;
  font-size: 11px;
  cursor: pointer;
  color: var(--on-primary, #fff);
  background: var(--primary);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.24);
  transition: background 160ms ease;
}
.ddm-logs-resume:hover { background: var(--primary-strong); }
.ddm-logs-resume:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }

.dl-log {
  display: grid;
  grid-template-columns: 62px 46px minmax(0, 1fr);
  gap: 8px;
  align-items: start;
  padding: 3px 6px;
  border-radius: 4px;
  color: var(--dl-log-color, #c9d6ec);
}

.dl-log-time {
  /* Same muted slate as the separation log's line numbers — on the dark console the theme's
     on-surface-muted is too close to the body text to recede. */
  color: #73839c;
  font-variant-numeric: tabular-nums;
  user-select: none;
}

.dl-log-level {
  text-transform: uppercase;
  font-size: 10px;
  font-weight: 600;
  text-align: center;
  line-height: 18px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--dl-log-color, #c9d6ec) 14%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--dl-log-color, #c9d6ec) 25%, transparent);
  user-select: none;
}

.dl-log-msg {
  min-width: 0;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.dl-log--info { --dl-log-color: #9dceff; }

.dl-log--warn {
  --dl-log-color: #f4d08b;
  background: color-mix(in srgb, var(--dl-log-color) 8%, transparent);
  box-shadow: inset 2px 0 0 color-mix(in srgb, var(--dl-log-color) 65%, transparent);
}

.dl-log--error {
  --dl-log-color: #f3aab8;
  background: color-mix(in srgb, var(--dl-log-color) 9%, transparent);
  box-shadow: inset 2px 0 0 color-mix(in srgb, var(--dl-log-color) 70%, transparent);
}

.ddm-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}
.ddm-footer-hint { flex: 1 1 240px; align-self: center; color: var(--on-surface-muted); font-size: 11px; line-height: 1.5; }

@media (max-width: 560px) {
  .download-detail-modal { width: calc(100vw - 24px); max-height: calc(100dvh - 24px); border-radius: 16px; }
  .download-detail-modal :deep(.n-card-header) { padding: 16px 16px 12px; }
  .download-detail-modal :deep(.n-card-footer),
  .download-detail-modal :deep(.n-card__footer) { padding: 12px 16px 16px; }
  .ddm-body { padding: 16px; gap: 14px; }
  .ddm-progress-pct { font-size: 26px; }
  .ddm-progress-info { gap: 16px; }
  .ddm-transfer-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
  .ddm-transfer-stat:first-child { grid-column: 1 / -1; }
  .dl-log { grid-template-columns: 54px 42px minmax(0, 1fr); gap: 6px; padding-inline: 3px; }
  .dl-log-time, .dl-log-msg { font-size: 11px; }
  .ddm-footer-hint { flex-basis: 100%; }
}
</style>
