<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { TerminalOutline } from '@vicons/ionicons5'
import { refreshToolLayout } from '../layout'

defineProps<{ busy?: boolean; percentage?: number | null }>()
const { t } = useI18n()
const show = ref(false)
function setOverlay(open: boolean) {
  document.documentElement.classList.toggle('tool-activity-open', open)
  window.parent.postMessage({ jsonrpc: '2.0', method: 'pymss.overlay', params: { open } }, '*')
  if (!open) refreshToolLayout()
}
watch(show, open => { if (open) setOverlay(true) })
onBeforeUnmount(() => setOverlay(false))
</script>
<template>
  <n-button class="tool-log-button" secondary size="small" :type="busy ? 'primary' : 'default'" @click="show = true">
    <template #icon><n-icon :component="TerminalOutline" /></template>
    {{ t('tools.logButton') }}<span v-if="busy && percentage != null" class="tool-log-percentage">{{ percentage }}%</span><span v-else-if="busy" class="tool-log-running" aria-hidden="true" />
  </n-button>
  <n-modal v-model:show="show" preset="card" :title="t('tools.activityLog')" class="tool-activity-dialog" :style="{ width: 'min(760px, calc(100vw - 32px))', maxHeight: 'calc(100dvh - 48px)' }" :content-style="{ overflow: 'auto', minHeight: '0' }" @after-leave="setOverlay(false)">
    <slot v-if="show" />
  </n-modal>
</template>
<style scoped>
.tool-log-button { gap: 4px; }
.tool-log-percentage { margin-left: 7px; font-variant-numeric: tabular-nums; }
.tool-log-running { width: 6px; height: 6px; margin-left: 8px; border-radius: 50%; background: currentColor; }
</style>
