<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useNumberInputAccessibility } from '@/composables/useNumberInputAccessibility'
import type { EditorMasterLimiter } from '@/types/editor'
import { normalizeMasterLimiter } from '@/utils/masterLimiter'
import EditorParameterSlider from './EditorParameterSlider.vue'

const props = defineProps<{ value?: EditorMasterLimiter }>()
const emit = defineEmits<{
  update: [patch: Partial<EditorMasterLimiter>]
  begin: []
  commit: []
}>()
const { t } = useI18n()
const vNumberA11y = useNumberInputAccessibility()
const limiter = computed(() => normalizeMasterLimiter(props.value))
function updateTime(key: 'attackMs' | 'releaseMs', value: number | null) {
  if (value !== null && Number.isFinite(value)) emit('update', { [key]: value })
}
</script>

<template>
  <section class="master-limiter" data-testid="master-limiter">
    <header>
      <strong>{{ t('editor.limiter.title') }}</strong>
      <n-switch size="small" :value="limiter.enabled" :aria-label="t('editor.limiter.enable')"
        @keydown.space.stop @update:value="emit('update', { enabled: $event })" />
    </header>
    <div class="master-limiter__times">
      <label>
        <span>{{ t('editor.limiter.attack') }} <small>ms</small></span>
        <n-input-number v-number-a11y="t('editor.limiter.attack') + ' (ms)'" :value="limiter.attackMs" :min="0.1" :max="100" :step="0.1" :precision="1"
          :disabled="!limiter.enabled" :update-value-on-input="false" size="small"
          @update:value="updateTime('attackMs', $event)" />
      </label>
      <label>
        <span>{{ t('editor.limiter.release') }} <small>ms</small></span>
        <n-input-number v-number-a11y="t('editor.limiter.release') + ' (ms)'" :value="limiter.releaseMs" :min="5" :max="2000" :step="5" :precision="0"
          :disabled="!limiter.enabled" :update-value-on-input="false" size="small"
          @update:value="updateTime('releaseMs', $event)" />
      </label>
    </div>
    <div v-for="field in (['gainDb', 'ceilingDb'] as const)" :key="field" class="master-limiter__parameter">
      <div class="master-limiter__label">
        <span>{{ t(field === 'gainDb' ? 'editor.limiter.gain' : 'editor.limiter.ceiling') }}</span>
        <output>{{ limiter[field].toFixed(1) }} dB</output>
      </div>
      <EditorParameterSlider :value="limiter[field]" :value-text="limiter[field].toFixed(1) + ' dB'" :reset-value="0" reset-label="0 dB"
        :label="t(field === 'gainDb' ? 'editor.limiter.gain' : 'editor.limiter.ceiling')"
        :min="field === 'gainDb' ? 0 : -30" :max="field === 'gainDb' ? 30 : 0" :step="0.1"
        :disabled="!limiter.enabled" :tooltip="false"
        @dragstart="emit('begin')" @dragend="emit('commit')"
        @update:value="emit('update', { [field]: $event })" />
    </div>
  </section>
</template>

<style scoped>
.master-limiter { display: grid; gap: 12px; padding: 12px; border: 1px solid var(--outline); border-radius: 8px; background: var(--surface-1); }
.master-limiter header, .master-limiter__label { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.master-limiter header { font-size: 12px; }
.master-limiter__times { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.master-limiter label, .master-limiter__parameter { display: grid; min-width: 0; gap: 6px; font-size: 11px; }
.master-limiter label > span { display: flex; align-items: baseline; gap: 4px; }
.master-limiter small { font-size: 10px; color: var(--on-surface-muted); }
.master-limiter output { font-variant-numeric: tabular-nums; }
</style>
