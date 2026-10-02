import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { parse } from 'vue/compiler-sfc'

const separate = parse(readFileSync(new URL('../src/views/SeparateView.vue', import.meta.url), 'utf8')).descriptor
const models = parse(readFileSync(new URL('../src/views/ModelsView.vue', import.meta.url), 'utf8')).descriptor
const alignedInput = parse(readFileSync(new URL('../src/components/AlignedInferenceInputNumber.vue', import.meta.url), 'utf8')).descriptor
const simpleWorkflow = parse(readFileSync(new URL('../src/components/workflow/WorkflowSimpleNodeEditor.vue', import.meta.url), 'utf8')).descriptor
const separateTemplate = separate.template?.content || ''
const modelsTemplate = models.template?.content || ''
const simpleWorkflowTemplate = simpleWorkflow.template?.content || ''

test('separation parameters use model-provided sample steps with existing fallbacks', () => {
  assert.ok(separate.scriptSetup?.content.includes('resolveInferenceSampleStep(currentModelInfo.value?.inferenceParamMeta, 1)'))
  assert.ok(separate.scriptSetup?.content.includes('resolveInferenceSampleStep(currentModelInfo.value?.inferenceParamMeta, 1024)'))
  assert.ok(separateTemplate.includes(':step="overlapSizeStep"'))
  assert.ok(separateTemplate.includes(':step="chunkSizeStep"'))
  assert.ok(separateTemplate.includes('@update:value="updateOverlapSize"'))
  assert.ok(separateTemplate.includes('@update:value="updateChunkSize"'))
})

test('model defaults editor uses the same sample-step metadata', () => {
  assert.ok(modelsTemplate.includes(':step="inferenceEditorOverlapStep"'))
  assert.ok(modelsTemplate.includes(':step="inferenceEditorChunkStep"'))
  assert.ok(modelsTemplate.includes("@update:value=\"updateInferenceDraftSize('overlap_size', $event)\""))
  assert.ok(modelsTemplate.includes("@update:value=\"updateInferenceDraftSize('chunk_size', $event)\""))
})

test('simple workflow model parameters use the same sample-step metadata', () => {
  const script = simpleWorkflow.scriptSetup?.content || ''
  assert.ok(script.includes('resolveInferenceSampleStep(inferenceEditorModel.value?.inferenceParamMeta, 1)'))
  assert.ok(script.includes('resolveInferenceSampleStep(inferenceEditorModel.value?.inferenceParamMeta, 1024)'))
  assert.ok(simpleWorkflowTemplate.includes(':step="inferenceEditorOverlapStep"'))
  assert.ok(simpleWorkflowTemplate.includes(':step="inferenceEditorChunkStep"'))
  assert.ok(simpleWorkflowTemplate.includes(':alignment-step="inferenceEditorSampleStep"'))
})

test('alignment is limited to step controls and leaves direct input untouched', () => {
  const script = alignedInput.scriptSetup?.content || ''
  assert.ok(script.includes("const button = target.closest('button')"))
  assert.ok(script.includes('const next = stepDirection'))
  assert.ok(script.includes("else stepDirection = 0"))
  assert.ok(script.includes("window.addEventListener('pointerup', scheduleDirectionReset)"))
})
