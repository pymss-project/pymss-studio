<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { logClock, logRecordRenderKeys, normalizeLogEntries, type LogEntry } from '@/utils/logRecord'
import { groupLogTracebacks, type LogRecordGroup } from '@/utils/logTracebacks'

const props = withDefaults(defineProps<{ lines: readonly LogEntry[]; compact?: boolean; embedded?: boolean }>(), { compact: false, embedded: false })
const { t } = useI18n()
const records = computed(() => normalizeLogEntries(props.lines))
const recordKeys = computed(() => logRecordRenderKeys(records.value))
const groups = computed(() => groupLogTracebacks(records.value))
const consoleRef = ref<HTMLElement | null>(null)
let followLogs = true
let resizeObserver: ResizeObserver | undefined

function scrollToBottom() {
  const element = consoleRef.value
  if (element && followLogs && !props.embedded) element.scrollTop = element.scrollHeight
}

function onScroll() {
  const element = consoleRef.value
  if (element && !props.embedded) {
    followLogs = element.scrollHeight - element.scrollTop - element.clientHeight < 40
  }
}

watch([consoleRef, () => props.embedded], ([element, embedded]) => {
  resizeObserver?.disconnect()
  followLogs = true
  if (!element || embedded) return
  resizeObserver = new ResizeObserver(scrollToBottom)
  resizeObserver.observe(element)
  scrollToBottom()
}, { flush: 'post' })
watch(groups, scrollToBottom, { flush: 'post' })
onBeforeUnmount(() => resizeObserver?.disconnect())

function lineNumber(group: LogRecordGroup) {
  const first = String(group.firstIndex + 1).padStart(3, '0')
  return group.lastIndex === group.firstIndex ? first : `${first}–${String(group.lastIndex + 1).padStart(3, '0')}`
}
function guardLogKeys(event: KeyboardEvent) {
  if (event.key !== 'Escape' && event.key !== 'Tab') event.stopPropagation()
}
</script>

<template>
  <div v-if="lines.length" ref="consoleRef" class="log-console" :class="{ 'log-console--compact': compact, 'log-console--embedded': embedded }" role="region" :aria-label="t('tasks.logs')" tabindex="0" @scroll.passive="onScroll" @wheel.stop @pointerdown.stop @dblclick.stop @contextmenu.stop @keydown="guardLogKeys">
    <div v-for="group in groups" :key="recordKeys[group.firstIndex]" class="log-line" :class="'log-line--' + (group.record.level || 'unclassified')">
      <span v-if="!compact" class="log-line-number">{{ lineNumber(group) }}</span>
      <span v-if="!compact" class="log-line-level">{{ group.record.level?.toUpperCase() || t('debug.logUnclassified') }}</span>
      <div class="log-line-text">
        <time v-if="group.record.timestamp" class="log-line-time" :datetime="group.record.timestamp">{{ logClock(group.record.timestamp) + ' ' }}</time><span>{{ group.message }}</span>
        <template v-for="(record, index) in group.records" :key="index">
          <pre v-if="record.detail" class="log-line-exception">{{ record.detail }}</pre>
        </template>
      </div>
    </div>
  </div>
  <div v-else class="log-empty">{{ t('tasks.noLogs') }}</div>
</template>

<style scoped>
.log-console {
  min-width: 0;
  user-select: text;
  max-height: min(62vh, 520px);
  overflow: auto;
  display: grid;
  gap: 2px;
  padding: 12px;
  border-radius: 12px;
  background: #0b1020;
  color: #c9d6ec;
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 400;
  line-height: 1.55;
  scrollbar-width: thin;
  scrollbar-color: color-mix(in srgb, #94a3b8 46%, transparent) transparent;
}

.log-console::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

.log-console::-webkit-scrollbar-track {
  background: transparent;
}

.log-console::-webkit-scrollbar-thumb {
  border: 2px solid #0b1020;
  border-radius: 999px;
  background: color-mix(in srgb, #94a3b8 44%, transparent);
  background-clip: padding-box;
}

.log-console::-webkit-scrollbar-thumb:hover {
  background: color-mix(in srgb, #94a3b8 64%, transparent);
  background-clip: padding-box;
}

.log-console::-webkit-scrollbar-corner {
  background: transparent;
}

.log-line {
  display: grid;
  grid-template-columns: 28px 46px minmax(0, 1fr);
  gap: 9px;
  min-width: 0;
  padding: 3px 6px;
  border-radius: 4px;
  color: var(--log-level-color, #c9d6ec);
}

.log-line--trace,
.log-line--debug { --log-level-color: #b8aed3; }
.log-line--unclassified { --log-level-color: #a0adc1; }

.log-line--info {
  --log-level-color: #9dceff;
}

.log-line--warn {
  --log-level-color: #f4d08b;
  background: color-mix(in srgb, var(--log-level-color) 8%, transparent);
  box-shadow: inset 2px 0 0 color-mix(in srgb, var(--log-level-color) 65%, transparent);
}

.log-line--error,
.log-line--fatal {
  --log-level-color: #f3aab8;
  background: color-mix(in srgb, var(--log-level-color) 9%, transparent);
  box-shadow: inset 2px 0 0 color-mix(in srgb, var(--log-level-color) 70%, transparent);
}

.log-line-number {
  color: #73839c;
  text-align: right;
  user-select: none;
}

.log-line-time { color: #94a3b8; font-variant-numeric: tabular-nums; }
.log-line-level {
  align-self: start;
  margin-top: 1px;
  border-radius: 4px;
  color: var(--log-level-color, #c9d6ec);
  background: color-mix(in srgb, var(--log-level-color, #c9d6ec) 14%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--log-level-color, #c9d6ec) 25%, transparent);
  font-size: 10px;
  font-weight: 600;
  line-height: 18px;
  text-align: center;
  user-select: none;
}

.log-line-text {
  display: block;
  min-width: 0;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.log-line-exception { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; }

.log-empty {
  padding: 18px;
  color: var(--on-surface-muted);
  text-align: center;
}

.log-console--compact { display: block; max-height: 200px; padding: 6px; border-radius: 6px; font-size: 11px; }
.log-console--compact .log-line { display: block; width: max-content; min-width: 100%; padding: 3px 4px; }
.log-console--compact .log-line-text,
.log-console--compact .log-line-exception { white-space: pre; overflow-wrap: normal; }
.log-console--embedded { max-height: none; overflow: visible; }
</style>
