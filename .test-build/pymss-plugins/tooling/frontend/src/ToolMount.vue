<script setup lang="ts">
import { onMounted, provide } from 'vue'
import Tool from '@tool/entry'
import { audioToolRuntimeKey, createAudioToolRuntime } from './features/audio-tools/runtime'

if (__USE_FILE_TOOL_RUNTIME__) {
  const runtime = createAudioToolRuntime()
  provide(audioToolRuntimeKey, runtime)
  onMounted(runtime.start)
}
</script>
<template><Tool /></template>
