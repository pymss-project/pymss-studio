<script setup lang="ts">
import OperationTool from '@/components/OperationTool.vue'
import config from './tool.json'
import AnalysisReport from './AnalysisReport.vue'
</script>
<template><OperationTool :config="config"><template #result="{ port, output, operation }"><AnalysisReport :name="port" :output="output" :operation="operation" /></template></OperationTool></template>
