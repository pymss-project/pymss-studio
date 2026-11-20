<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { InferencePreset } from '@/features/inference/presets'

withDefaults(defineProps<{
  presets?: readonly InferencePreset[]
  activePreset?: string
  presetsDisabled?: boolean
  tta?: boolean
  ttaDisabled?: boolean
  ttaGuided?: boolean
  resetLabel?: string
  resetDisabled?: boolean
  applyDisabled?: boolean
  applying?: boolean
}>(), { tta: undefined })
const emit = defineEmits<{
  close: []
  reset: []
  apply: []
  preset: [preset: InferencePreset]
  'update:tta': [value: boolean]
}>()
const { t } = useI18n()
</script>

<template>
  <n-card class="settings-modal" :title="t('separate.settingsDrawerTitle')" :aria-label="t('separate.settingsDrawerTitle')"
    :bordered="false" closable role="dialog" aria-modal="true" @close="emit('close')">
    <div class="settings-drawer__content">
      <section class="settings-inference">
        <div class="parameter-section-heading">
          <div class="parameter-section-heading__title">
            <h3>{{ t('parameterEditor.inferenceGroup') }}</h3>
            <div v-if="presets?.length" class="parameter-presets" data-parameter-guide="presets"
              role="group" :aria-label="t('parameterEditor.presets.label')">
              <n-button v-for="preset in presets" :key="preset.id" size="tiny" secondary
                :type="activePreset === preset.id ? 'primary' : 'default'" :aria-pressed="activePreset === preset.id"
                :disabled="presetsDisabled" @click="emit('preset', preset)">{{ t(preset.label) }}</n-button>
            </div>
          </div>
          <div v-if="$slots.source" class="parameter-section-heading__actions" data-parameter-guide="parameterSource">
            <slot name="source" />
          </div>
        </div>
        <div v-if="$slots.context" class="parameter-context"><slot name="context" /></div>
        <slot />
        <div v-if="tta !== undefined" class="settings-tta" data-parameter-guide="tta"
          :class="{ 'parameter-guide-target--active': ttaGuided }">
          <span>{{ t('separate.tta') }}<slot name="tta-label" /></span>
          <slot name="tta-actions" />
          <n-switch :value="tta" :aria-label="t('separate.tta')" :disabled="ttaDisabled" size="small"
            @update:value="(value: boolean) => emit('update:tta', value)" />
        </div>
      </section>
      <slot name="output" />
    </div>
    <slot name="overlay" />

    <template #footer>
      <slot name="footer-hint" />
      <div class="parameter-modal-footer">
        <n-button secondary :disabled="applying || resetDisabled" @click="emit('reset')">{{ resetLabel || t('parameterEditor.restoreDefaults') }}</n-button>
        <n-button secondary :disabled="applying" @click="emit('close')">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :loading="applying" :disabled="applyDisabled" @click="emit('apply')">{{ t('parameterEditor.apply') }}</n-button>
      </div>
    </template>
  </n-card>
</template>

<style scoped>
.settings-modal {
  width: min(700px, calc(100vw - 48px));
  max-height: min(calc(100dvh - 88px), 840px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-radius: 16px;
}
.settings-modal :deep(.n-card-header) { flex: 0 0 auto; padding: 16px 24px 0; }
.settings-modal :deep(.n-card-header__main) { font-size: 17px; font-weight: 600; }
.settings-modal :deep(.n-card-content), .settings-modal :deep(.n-card__content) {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  padding: 0 24px 12px;
}
.settings-modal :deep(.n-card-footer), .settings-modal :deep(.n-card__footer) {
  flex: 0 0 auto;
  position: relative;
  padding: 12px 24px;
  border-top: 1px solid color-mix(in srgb, var(--outline) 86%, transparent);
  background: color-mix(in srgb, var(--surface-1) 96%, transparent);
}
.settings-drawer__content { display: grid; gap: 24px; min-height: 0; padding-top: 14px; }
.settings-inference { display: grid; gap: 10px; min-width: 0; }
.parameter-section-heading { display: flex; align-items: center; flex-wrap: wrap; justify-content: space-between; gap: 6px 12px; min-height: 28px; }
.parameter-section-heading h3 { flex-shrink: 0; margin: 0; font-size: 14px; font-weight: 600; color: var(--on-surface-heading); }
.parameter-section-heading__title { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 12px; min-width: 0; }
.parameter-presets { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.parameter-section-heading__actions { display: flex; align-items: center; gap: 4px; min-width: 0; }
.parameter-context { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 10px; margin-top: -6px; }
.parameter-context :slotted(p) { margin: 0; font-size: 11px; font-weight: 400; line-height: 1.5; color: var(--on-surface-muted); }
.settings-tta { display: flex; align-items: center; gap: 8px; min-height: 44px; font-size: 13px; font-weight: 500; color: var(--on-surface); }
.settings-tta.parameter-guide-target--active { position: relative; }
.settings-tta .n-switch { margin-inline-start: auto; }
.parameter-modal-footer { display: flex; justify-content: flex-end; align-items: center; flex-wrap: wrap; gap: 8px; }

@media (max-width: 640px) {
  .settings-modal { width: min(700px, calc(100vw - 32px)); }
  .settings-modal :deep(.n-card-header),
  .settings-modal :deep(.n-card-content),
  .settings-modal :deep(.n-card__content),
  .settings-modal :deep(.n-card-footer),
  .settings-modal :deep(.n-card__footer) { padding-inline: 16px; }
}
</style>
