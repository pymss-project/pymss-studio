<script setup lang="ts">
import { computed, useAttrs } from 'vue'
import { NIcon } from 'naive-ui'

// Templates resolve `<n-icon>` to this component (see vite.config.ts). Naive UI renders every
// icon as an unnamed `role="img"`, which screen readers announce as an unlabeled image. Icons
// are decorative unless they carry their own accessible name.
defineOptions({ inheritAttrs: false })

const attrs = useAttrs()
const labelled = computed(() => attrs['aria-label'] != null || attrs['aria-labelledby'] != null)
</script>

<template>
  <NIcon v-bind="attrs" :aria-hidden="labelled ? undefined : 'true'"><slot /></NIcon>
</template>
