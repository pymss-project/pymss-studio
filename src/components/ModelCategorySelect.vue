<script setup lang="ts">
import { h, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { TreeSelectNodeProps, TreeSelectProps, TreeSelectRenderSuffix } from 'naive-ui'
import { useSettingsStore } from '@/stores/settings'
import type { ModelCategoryOption } from '@/utils/modelCategory'

const props = withDefaults(defineProps<{
  value: string
  options: ModelCategoryOption[]
  size?: 'small' | 'medium' | 'large'
}>(), { size: 'medium' })
const emit = defineEmits<{ 'update:value': [value: string] }>()
const { t } = useI18n()
const settings = useSettingsStore()
const expandedKeys = ref<string[]>([])
const nodeProps: TreeSelectNodeProps = ({ option }) => ({ title: String(option.label || '') })
const renderSelectedMark: TreeSelectRenderSuffix = ({ selected }) => selected ? h('span', { 'aria-hidden': 'true' }, '✓') : null

const themeOverrides = {
  menuPadding: 'var(--select-menu-padding)',
  menuHeight: 'min(var(--select-menu-max-height), 45dvh)',
  menuBorderRadius: 'var(--select-menu-radius)',
  menuColor: 'var(--surface-1)',
  menuBoxShadow: 'var(--shadow-panel)',
  peers: {
    Tree: {
      fontSize: '14px',
      nodeWrapperPadding: '0',
      nodeBorderRadius: 'var(--select-option-radius)',
      nodeColorActive: 'var(--primary-softer)',
      nodeTextColor: 'var(--on-surface)',
      arrowColor: 'var(--on-surface-muted)',
      peers: {
        Scrollbar: { color: 'var(--outline-strong)', colorHover: 'var(--on-surface-muted)', width: '6px' },
      },
    },
  },
} satisfies NonNullable<TreeSelectProps['themeOverrides']>

watch([() => props.value, () => props.options], () => {
  const parent = props.options.find(option => option.children?.some(child => child.value === props.value))
  if (parent && !expandedKeys.value.includes(parent.value)) expandedKeys.value.push(parent.value)
}, { immediate: true })
</script>

<template>
  <n-tree-select
    :value="value"
    :options="options"
    :size="size"
    key-field="value"
    show-path
    separator=" › "
    :indent="20"
    :consistent-menu-width="false"
    :expanded-keys="expandedKeys"
    :menu-props="{ class: ['model-category-menu', { 'model-category-menu--animated': settings.animationsEnabled }] }"
    :node-props="nodeProps"
    :render-suffix="renderSelectedMark"
    :theme-overrides="themeOverrides"
    :aria-label="t('models.category')"
    @update:value="emit('update:value', typeof $event === 'string' ? $event : '')"
    @update:expanded-keys="expandedKeys = $event.map(String)"
  />
</template>

<style lang="scss">
.n-tree-select-menu.model-category-menu {
  width: min(calc(100vw - 2rem), max(16rem, var(--v-target-width, 0px)));
  border: 1px solid var(--outline);
  --select-enter-y: -3px;

  .n-tree-node {
    align-items: center;
    min-height: var(--select-option-height);
  }

  .n-tree-node-switcher {
    min-width: 24px;
    height: var(--select-option-height);
  }

  .n-tree-node-content { gap: 8px; padding-inline-end: 9px; }
  .n-tree-node-content__text {
    min-width: 0;
    overflow: hidden;
    border: 0;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .n-tree-node-content__suffix { flex: 0 0 1em; justify-content: flex-end; }

  .n-tree-node--selected .n-tree-node-content { color: var(--primary-strong); }
  .n-tree-node:not(.n-tree-node--disabled):is(:hover, .n-tree-node--pending) { background: var(--primary-soft); }

  @media (forced-colors: active) {
    border-color: ButtonText;
    .n-tree-node--pending { outline: 1px solid Highlight; outline-offset: -1px; }
  }

  [v-placement^='top'] & { --select-enter-y: 3px; }

  &.fade-in-scale-up-transition-enter-active,
  &.fade-in-scale-up-transition-leave-active { transition: none; }

  &.fade-in-scale-up-transition-enter-from { opacity: 1; transform: none; }
  &.fade-in-scale-up-transition-leave-active { visibility: hidden; pointer-events: none; }

  @media (prefers-reduced-motion: no-preference) {
    &.model-category-menu--animated {
      &.fade-in-scale-up-transition-enter-active {
        transition: opacity var(--select-enter-motion), transform var(--select-enter-motion);
      }

      &.fade-in-scale-up-transition-enter-from {
        opacity: 0;
        transform: translateY(var(--select-enter-y));
      }

      &.fade-in-scale-up-transition-leave-active {
        visibility: visible;
        transition: opacity var(--select-leave-motion);
      }

      &.fade-in-scale-up-transition-leave-from,
      &.fade-in-scale-up-transition-leave-to { transform: none; }
    }
  }
}
</style>
