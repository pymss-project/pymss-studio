<script setup lang="ts">
import { useSlots } from 'vue'

defineProps<{
  /** 状态，用于圆点样式修饰符（downloading / paused / error / interrupted / done ...） */
  status: string
  label?: string
  /** 状态文案 */
  message: string
  /** 进度百分比 0-100 */
  progress: number
  /** 可选的文件进度行文案（如“3 / 5”），为空则不渲染 */
  filesText?: string
  /** 可选的进度条颜色，缺省使用组件默认色 */
  color?: string
}>()

const slots = useSlots()
</script>

<template>
  <div class="mc-dl">
    <div class="mc-dl-info">
      <div class="mc-dl-status">
        <span :class="['mc-dl-dot', `mc-dl-dot--${status}`]" />
        <span class="mc-dl-msg">{{ message }}</span>
      </div>
      <span class="mc-dl-pct">{{ progress }}%</span>
    </div>
    <n-progress
      :aria-label="label || message"
      :aria-valuetext="`${progress}% · ${message}${filesText ? ' · ' + filesText : ''}`"
      :percentage="progress"
      :show-indicator="false"
      :height="10"
      :border-radius="5"
      type="line"
      :color="color"
      rail-color="var(--surface-3)"
    />
    <div v-if="filesText" class="mc-dl-files">{{ filesText }}</div>
    <div v-if="slots.actions" class="mc-dl-actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<style scoped>
.mc-dl {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  min-width: 0;
}

.mc-dl-info {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 6px;
  width: 100%;
  min-width: 0;
}

.mc-dl-status {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1 1 auto;
  min-width: 0;
}

.mc-dl-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  background: var(--primary);
  animation: mc-dl-pulse 1.5s infinite;
}

.mc-dl-dot--downloading {
  background: var(--primary);
}

.mc-dl-dot--preparing {
  background: var(--primary);
  animation: mc-dl-pulse 1.5s infinite;
}

.mc-dl-dot--error {
  background: var(--danger);
  animation: none;
}

.mc-dl-dot--paused,
.mc-dl-dot--cancelled,
.mc-dl-dot--interrupted {
  background: var(--warning);
  animation: none;
}

.mc-dl-dot--done {
  background: var(--success);
  animation: none;
}

@keyframes mc-dl-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

.mc-dl-msg {
  font-size: 11px;
  color: var(--on-surface-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mc-dl-pct {
  font-size: 13px;
  font-weight: 600;
  color: var(--on-surface);
  flex-shrink: 0;
  margin-left: 8px;
}

.mc-dl-files {
  font-size: 11px;
  color: var(--on-surface-muted);
}

.mc-dl-actions {
  display: flex;
  gap: 6px;
  align-items: center;
}
</style>
