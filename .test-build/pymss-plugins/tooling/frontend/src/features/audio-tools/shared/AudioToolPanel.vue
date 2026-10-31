<script setup lang="ts">
import { computed } from 'vue'
import type { AudioToolKey, AudioToolResult } from '../types'
import AudioToolStatus from '@/components/tools/AudioToolStatus.vue'
import ToolPanelShell from '@/components/ToolPanelShell.vue'
import ToolActivityDialog from '@/components/ToolActivityDialog.vue'
import { useAudioToolRuntime } from '../runtime'

const props = defineProps<{ tool: AudioToolKey; statusTitle: string; statusHint: string }>()
const runtime = useAudioToolRuntime()
const state = runtime.stateFor(props.tool)
const showResult = computed(() => state.value.hasResult || Boolean(state.value.error) || state.value.progress.phase === 'cancelled')
</script>
<template>
  <ToolPanelShell layout="stacked" :status-title="statusTitle" :status-hint="statusHint" :show-status="showResult">
    <template #actions>
      <ToolActivityDialog :busy="state.busy" :percentage="state.progress.total > 0 ? state.percentage : null">
        <AudioToolStatus view="activity" :busy="state.busy" :cancelling="state.cancelling" :has-result="state.hasResult" :error="state.error" :progress="state.progress" :percentage="state.percentage" :result="state.result" :elapsed-ms="state.elapsedMs" :logs="state.logs" @cancel="runtime.cancel" />
      </ToolActivityDialog>
    </template>
    <slot :state="state" />
    <template #status>
      <n-alert v-if="state.error" type="error" :bordered="false">{{ state.error }}</n-alert>
      <n-alert v-else-if="state.progress.phase === 'cancelled'" type="info" :bordered="false">{{ $t('tasks.statusCancelled') }}</n-alert>
      <AudioToolStatus v-else view="result" :busy="state.busy" :cancelling="state.cancelling" :has-result="state.hasResult" :error="state.error" :progress="state.progress" :percentage="state.percentage" :result="state.result" :elapsed-ms="state.elapsedMs" :logs="state.logs" @reveal="runtime.revealPath">
        <template v-if="$slots.result" #result="slotProps: { result: AudioToolResult }"><slot name="result" :result="slotProps.result" /></template>
      </AudioToolStatus>
    </template>
  </ToolPanelShell>
</template>
