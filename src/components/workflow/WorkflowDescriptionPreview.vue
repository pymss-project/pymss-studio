<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

const props = withDefaults(defineProps<{ text: string; lines?: 1 | 2 }>(), { lines: 2 })
const emit = defineEmits<{ view: [] }>()
const { t } = useI18n()
const content = ref<HTMLElement | null>(null)
const overflowing = ref(false)
const preview = computed(() => props.text.replace(/\s+/g, ' ').trim())
let observer: ResizeObserver | undefined
function measure() {
  const element = content.value
  overflowing.value = !!element && (element.scrollHeight > element.clientHeight + 1 || element.scrollWidth > element.clientWidth + 1)
}
onMounted(() => {
  observer = new ResizeObserver(measure)
  if (content.value) observer.observe(content.value)
  measure()
})
watch([preview, () => props.lines], async () => { await nextTick(); measure() })
onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <div class="workflow-description-preview">
    <p ref="content" class="workflow-description-preview__text" :class="{ 'workflow-description-preview__text--single': lines === 1 }">
      {{ preview || t('workflows.descriptionEmpty') }}
    </p>
    <n-button v-if="overflowing" class="workflow-description-preview__more" text type="primary" size="tiny" @click.stop="emit('view')">{{ t('workflows.viewFullDescription') }}</n-button>
  </div>
</template>

<style scoped>
.workflow-description-preview { min-width: 0; display: grid; gap: 2px; }
.workflow-description-preview__text { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; max-height: 3.3em; margin: 0; overflow: hidden; overflow-wrap: anywhere; color: var(--on-surface-muted); font-size: 13px; line-height: 1.65; }
.workflow-description-preview__text--single { display: block; max-height: 1.65em; white-space: nowrap; text-overflow: ellipsis; }
.workflow-description-preview__more { justify-self: start; min-height: 32px; }
</style>
