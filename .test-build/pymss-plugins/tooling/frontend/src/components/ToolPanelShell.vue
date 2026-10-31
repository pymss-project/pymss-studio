<script setup lang="ts">
withDefaults(defineProps<{
  statusTitle: string
  statusHint: string
  layout?: 'split' | 'stacked'
  showStatus?: boolean
}>(), { layout: 'split', showStatus: true })
</script>
<template>
  <div class="audio-tool-layout" :class="{ 'audio-tool-layout--stacked': layout === 'stacked' }">
    <section class="audio-tool-card audio-tool-card--main" :class="{ 'audio-tool-card--with-actions': $slots.actions }">
      <div v-if="$slots.actions" class="audio-tool-header-actions"><slot name="actions" /></div>
      <slot />
    </section>
    <aside v-if="showStatus" class="audio-tool-card audio-tool-card--side">
      <h3>{{ statusTitle }}</h3>
      <p>{{ statusHint }}</p>
      <slot name="status" />
    </aside>
  </div>
</template>
