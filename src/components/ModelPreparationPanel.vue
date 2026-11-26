<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { useRouter } from 'vue-router'
import { useModelStore } from '@/stores/model'
import { useAppStore } from '@/stores/app'
import { buildModelCategoryOptionsFromModels, matchesModelCategory } from '@/utils/modelCategory'
import { compareModelDefaultOrder } from '@/utils/modelSort'
import { formatBytes } from '@/utils/format'
import { isActiveDownloadStatus } from '@/utils/downloadQueue'
import AppSelect from '@/components/AppSelect.vue'
import ModelCategorySelect from '@/components/ModelCategorySelect.vue'
import ModelProgressBlock from '@/components/ModelProgressBlock.vue'

const { t, locale } = useI18n()
const model = useModelStore()
const app = useAppStore()
const message = useMessage()
const router = useRouter()
const category = ref('')
const selectedName = ref('')
const pendingName = ref('')
const submitting = ref(false)
const candidates = computed(() => model.models.filter(item => item.supported && item.source !== 'user' && !item.downloaded))
const categories = computed(() => buildModelCategoryOptionsFromModels(candidates.value, locale.value, t('common.all')))
const options = computed(() => candidates.value.filter(item => matchesModelCategory(item, category.value))
  .sort((a, b) => compareModelDefaultOrder(a, b, (left, right) => left.localeCompare(right, locale.value)))
  .map(item => ({ label: item.name, value: item.name })))
const selected = computed(() => model.models.find(item => item.name === selectedName.value))
const download = computed(() => model.downloadTasks[selectedName.value])
const busy = computed(() => submitting.value || isActiveDownloadStatus(download.value?.status))
const runtimeMissing = computed(() => app.runtimeInfo !== null && app.runtimeInfo !== undefined && !app.runtimeInfo.ready)
const progress = computed(() => Math.max(0, Math.min(100, Math.round(download.value?.progress || 0))))
const downloadLabel = computed(() => download.value?.status === 'preparing'
  ? t('modelPreparation.preparing') : t('modelPreparation.downloading'))
const resumable = computed(() => ['paused', 'interrupted'].includes(download.value?.status || ''))

watch(options, items => {
  if (!busy.value && !items.some(item => item.value === selectedName.value)) selectedName.value = ''
})
watch(() => model.models.find(item => item.name === pendingName.value)?.downloaded, async ready => {
  if (!ready || !pendingName.value) return
  const name = pendingName.value
  pendingName.value = ''
  try { await model.selectModel(name) }
  catch (error) { message.error(error instanceof Error ? error.message : String(error)) }
}, { flush: 'sync' })
watch(() => model.downloadTasks[pendingName.value]?.status, status => {
  if (status === 'error' || status === 'cancelled') pendingName.value = ''
})

async function prepareModel() {
  if (!selected.value || busy.value || runtimeMissing.value) return
  const name = selected.value.name
  submitting.value = true
  pendingName.value = name
  try {
    const started = await model.downloadModel(name)
    if (!started && !isActiveDownloadStatus(model.downloadTasks[name]?.status)) pendingName.value = ''
  } catch (error) {
    pendingName.value = ''
    message.error(error instanceof Error ? error.message : String(error))
  } finally { submitting.value = false }
}

async function pauseDownload() {
  pendingName.value = ''
  try { await model.cancelDownload(selectedName.value, true) }
  catch (error) { message.error(error instanceof Error ? error.message : String(error)) }
}
</script>

<template>
  <section class="model-preparation" :aria-label="t('modelPreparation.title')">
    <strong>{{ t('modelPreparation.title') }}</strong>
    <p>{{ t('modelPreparation.description') }}</p>
    <p v-if="runtimeMissing" role="status">{{ t('modelPreparation.runtimeRequired') }}</p>
    <n-button v-if="runtimeMissing" secondary @click="router.push({ path: '/settings', query: { section: 'runtime' } })">{{ t('taskRecovery.runtimeAction') }}</n-button>
    <template v-if="candidates.length || busy">
      <span class="model-preparation__label">{{ t('modelPreparation.purpose') }}</span>
      <ModelCategorySelect v-model:value="category" :options="categories" :disabled="busy" />
      <label for="first-model">{{ t('separate.model') }}</label>
      <AppSelect id="first-model" v-model:value="selectedName" :options="options" :disabled="busy" filterable
        :placeholder="t('modelPreparation.selectModel')" :aria-label="t('modelPreparation.selectModel')" />
      <p v-if="selected">{{ t('modelPreparation.downloadSize', { size: formatBytes(selected.sizeBytes) }) }}</p>
      <ModelProgressBlock v-if="busy" :status="download?.status || 'preparing'" :progress="progress" :message="downloadLabel">
        <template #actions><n-button size="small" :disabled="submitting" @click="pauseDownload">{{ t('common.pause') }}</n-button></template>
      </ModelProgressBlock>
      <p v-else-if="download?.status === 'error'" role="alert">{{ model.downloadErrors[selectedName] || download.errorMessage }}</p>
      <p v-else-if="resumable" role="status">{{ t('modelPreparation.paused') }}</p>
      <n-button v-if="!runtimeMissing" type="primary" :disabled="!selected || busy" @click="prepareModel">
        {{ resumable ? t('modelPreparation.resume') : t('modelPreparation.download') }}
      </n-button>
    </template>
    <n-button quaternary @click="router.push('/models')">{{ t('separate.modelPanelPrimaryAction') }}</n-button>
  </section>
</template>

<style scoped>
.model-preparation { display: grid; gap: 10px; width: min(100%, 540px); padding: 18px; margin: 0 auto; text-align: left; }
.model-preparation p { margin: 0; font-size: 13px; color: var(--on-surface-muted); overflow-wrap: anywhere; }
.model-preparation label, .model-preparation__label { font-size: 13px; font-weight: 600; }
</style>
