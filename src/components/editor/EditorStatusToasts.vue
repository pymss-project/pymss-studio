<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { CheckmarkCircleOutline, InformationCircleOutline, RemoveCircleOutline } from '@vicons/ionicons5'

defineProps<{ animate: boolean }>()

const MAX_VISIBLE = 4
const DISPLAY_MS = 2600
const items = ref<{ id: number; message: string; enabled?: boolean }[]>([])
const timers = new Map<number, number>()
let nextId = 1

function dismiss(id: number) {
  window.clearTimeout(timers.get(id))
  timers.delete(id)
  items.value = items.value.filter((item) => item.id !== id)
}

function show(message: string, enabled?: boolean) {
  if (items.value.length >= MAX_VISIBLE) dismiss(items.value[0].id)
  const id = nextId++
  items.value.push({ id, message, enabled })
  timers.set(id, window.setTimeout(() => dismiss(id), DISPLAY_MS))
}

function clear() {
  for (const timer of timers.values()) window.clearTimeout(timer)
  timers.clear()
  items.value = []
}

onBeforeUnmount(clear)
defineExpose({ show, clear })
</script>

<template>
  <div class="editor-status-toasts" role="status" aria-atomic="false" aria-relevant="additions">
    <TransitionGroup name="editor-status-toast" :css="animate">
      <div v-for="item in items" :key="item.id" class="editor-status-toast">
        <n-icon
          :component="item.enabled === undefined ? InformationCircleOutline : item.enabled ? CheckmarkCircleOutline : RemoveCircleOutline"
          :class="{ 'editor-status-toast__enabled': item.enabled }"
          aria-hidden="true"
        />
        <span>{{ item.message }}</span>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.editor-status-toasts {
  position: absolute;
  z-index: 12;
  left: 20px;
  bottom: 16px;
  display: flex;
  max-width: min(300px, calc(100% - 40px));
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
  pointer-events: none;
}

.editor-status-toast {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  padding: 7px 11px;
  border: 1px solid var(--outline);
  border-radius: 8px;
  background: var(--surface);
  color: var(--on-surface);
  box-shadow: 0 2px 7px rgb(0 0 0 / 13%);
  font-size: 12px;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.editor-status-toast :deep(.n-icon) {
  color: var(--on-surface-muted);
  flex: none;
  font-size: 15px;
}

.editor-status-toast :deep(.editor-status-toast__enabled) {
  color: var(--primary-strong);
}

.editor-status-toast-enter-active,
.editor-status-toast-leave-active {
  transition: opacity 140ms ease, transform 140ms ease;
}

.editor-status-toast-enter-from,
.editor-status-toast-leave-to {
  opacity: 0;
  transform: translateY(4px);
}

@media (prefers-reduced-motion: reduce) {
  .editor-status-toast-enter-active,
  .editor-status-toast-leave-active {
    transition: none;
  }
}
</style>
