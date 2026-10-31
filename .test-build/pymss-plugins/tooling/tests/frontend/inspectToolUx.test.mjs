import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { parse } from 'vue/compiler-sfc'

function descriptor(path) {
  return parse(readFileSync(new URL(path, import.meta.url), 'utf8')).descriptor
}

const inspect = descriptor('../../../plugins/inspect/ui/src/InspectTool.vue')
const panel = descriptor('../../frontend/src/features/audio-tools/shared/AudioToolPanel.vue')
const status = descriptor('../../frontend/src/components/tools/AudioToolStatus.vue')
const shell = descriptor('../../frontend/src/components/ToolPanelShell.vue')
const inspectTemplate = inspect.template?.content || ''
const inspectScript = inspect.scriptSetup?.content || ''
const panelTemplate = panel.template?.content || ''
const panelScript = panel.scriptSetup?.content || ''
const statusTemplate = status.template?.content || ''
const statusScript = status.scriptSetup?.content || ''

test('inspect keeps file selection and its primary action in one compact row', () => {
  assert.ok(inspectTemplate.includes('class="audio-tool-path"'))
  assert.ok(inspectTemplate.includes("t('tools.startInspect')"))
  assert.ok(inspectTemplate.includes('class="inspect-privacy"'))
  assert.ok(!inspectTemplate.includes('<n-alert'))
  assert.ok(!inspectTemplate.includes('class="audio-tool-actions"'))
  assert.ok(inspectScript.includes('inputPath.value = path; await inspect(path)'))
})

test('idle tools reserve no progress panel and keep results outside the activity dialog', () => {
  assert.ok(panelScript.includes('const showResult = computed(() => state.value.hasResult'))
  assert.ok(panelTemplate.includes(':show-status="showResult"'))
  assert.ok(shell.template.content.includes('v-if="showStatus"'))
  assert.ok(panelTemplate.indexOf('view="activity"') < panelTemplate.indexOf('</ToolActivityDialog>'))
  assert.ok(panelTemplate.indexOf('view="result"') > panelTemplate.indexOf('</ToolActivityDialog>'))
  assert.ok(panelTemplate.includes('v-if="state.error"'))
})

test('status rendering has separate result and activity views', () => {
  assert.ok(statusTemplate.includes(`v-if="view === 'activity'"`))
  assert.ok(statusTemplate.includes(`v-if="view === 'result'"`))
  assert.ok(!statusScript.includes('compact?:'))
})
