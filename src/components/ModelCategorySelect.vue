<script setup lang="ts">
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { TreeSelectOption } from 'naive-ui'
import type { ModelCategoryOption } from '@/utils/modelCategory'

const props = withDefaults(defineProps<{
  value: string
  options: ModelCategoryOption[]
  size?: 'small' | 'medium' | 'large'
}>(), { size: 'medium' })
const emit = defineEmits<{ 'update:value': [value: string] }>()
const { t } = useI18n()
const expandedKeys = ref<string[]>([])
const nodeProps = ({ option }: { option: TreeSelectOption }) => ({ title: String(option.label || '') })

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
    :menu-props="{ class: 'model-category-menu' }"
    :node-props="nodeProps"
    :aria-label="t('models.category')"
    @update:value="emit('update:value', typeof $event === 'string' ? $event : '')"
    @update:expanded-keys="expandedKeys = $event.map(String)"
  />
</template>

<style lang="scss">
.model-category-menu {
  width: min(22rem, calc(100vw - 2rem));
}
</style>
