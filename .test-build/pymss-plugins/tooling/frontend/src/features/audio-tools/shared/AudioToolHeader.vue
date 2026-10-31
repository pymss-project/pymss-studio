<script setup lang="ts">
import type { Component } from 'vue'
defineProps<{ icon: Component; title: string; description: string }>()
</script>
<template>
  <div class="audio-tool-header">
    <div class="audio-tool-icon"><n-icon :component="icon" /></div>
    <div><h2>{{ title }}</h2><p>{{ description }}</p></div>
  </div>
</template>
