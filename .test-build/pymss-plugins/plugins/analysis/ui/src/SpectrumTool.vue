<script setup lang="ts">
import OperationTool from '@/components/OperationTool.vue'
import AnalysisReport from './AnalysisReport.vue'
const config = {
  titleKey: 'plugin.operations.spectrum',
  descriptionKey: 'analysis.spectrumDescription',
  operations: [{ id: 'pymss.analysis.spectrum', titleKey: 'plugin.operations.spectrum', inputs: ['audio'] }],
}
</script>
<template><OperationTool :config="config"><template #result="{ port, output, operation }"><AnalysisReport :name="port" :output="output" :operation="operation" /></template></OperationTool></template>
