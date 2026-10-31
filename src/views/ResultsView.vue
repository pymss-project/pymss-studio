<script setup lang="ts">
import { computed, h, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  CheckmarkOutline,
  ChevronDownOutline,
  FolderOpenOutline,
  FolderOutline,
  DocumentTextOutline,
  ColorWandOutline,
  SearchOutline,
  SwapVerticalOutline,
  TimeOutline,
  TrashOutline,
} from '@vicons/ionicons5'
import { useTaskStore, type SeparationJob, type SeparationTask } from '@/stores/task'
import { useEditorStore, type OrphanedEditorProject } from '@/stores/editor'
import { usePagedSelection } from '@/composables/usePagedSelection'
import { resultType, RESULT_TYPE_KEYS, type ResultType } from '@/utils/resultType'
import { isAudioOutput } from '@/utils/stemOutputs'
import { NCheckbox, useDialog, useMessage } from 'naive-ui'

type ResultSort = 'time_desc' | 'time_asc' | 'name_asc' | 'name_desc'
type ResultGroup = SeparationJob & { items: SeparationTask[]; resultType: ResultType }

const { t } = useI18n()
const task = useTaskStore()
const editor = useEditorStore()
const message = useMessage()
const dialog = useDialog()

const search = ref('')
const typeFilter = ref<ResultType | 'all'>('all')
const typeOptions = computed(() => [
  { label: t('results.typeAll'), value: 'all' },
  ...Object.entries(RESULT_TYPE_KEYS).filter(([value]) => value !== 'unknown' || resultGroups.value.some(group => group.resultType === 'unknown'))
    .map(([value, key]) => ({ label: t(key), value })),
])
const sortBy = ref<ResultSort>('time_desc')
const expandedIds = ref<string[]>([])
const orphanedEditorProjects = ref<OrphanedEditorProject[]>([])
const orphanedProjectsLoading = ref(false)
const orphanedProjectsCleaning = ref(false)

const sortOptions = [
  { label: t('results.sortTimeDesc'), value: 'time_desc' },
  { label: t('results.sortTimeAsc'), value: 'time_asc' },
  { label: t('results.sortNameAsc'), value: 'name_asc' },
  { label: t('results.sortNameDesc'), value: 'name_desc' },
]

const resultGroups = computed<ResultGroup[]>(() => {
  return task.resultJobs.map((job) => ({ ...job, items: job.tasks, resultType: resultType(job.primary) }))
})
const filteredResults = computed(() => {
  const keyword = search.value.trim().toLowerCase()
  const list = resultGroups.value.filter((group) => {
    if (typeFilter.value !== 'all' && group.resultType !== typeFilter.value) return false
    if (!keyword) return true
    const haystack = [
      group.id,
      group.model,
      group.output,
      ...group.items.map(item => getFileName(item.input)),
      ...group.items.map(item => item.output),
      ...group.items.flatMap(item => item.outputs.map((output) => output.stem)),
      ...group.items.flatMap(item => item.outputs.map((output) => output.path)),
    ].join(' ').toLowerCase()
    return haystack.includes(keyword)
  })

  return [...list].sort((a, b) => {
    switch (sortBy.value) {
      case 'time_asc':
        return a.updatedAt - b.updatedAt
      case 'name_asc':
        return getGroupTitle(a).localeCompare(getGroupTitle(b), 'zh-CN')
      case 'name_desc':
        return getGroupTitle(b).localeCompare(getGroupTitle(a), 'zh-CN')
      case 'time_desc':
      default:
        return b.updatedAt - a.updatedAt
    }
  })
})
const pagedSelection = usePagedSelection(filteredResults, {
  initialPageSize: 24,
  pageSizeOptions: [12, 24, 48, 96],
})
const {
  selecting,
  selectedIds: selectedResultIds,
  selectedSet: selectedResultSet,
  page,
  pageSize,
  pageSizeOptions,
  allSelected: allResultsSelected,
  someSelected: someResultsSelected,
  toggleSelecting,
  toggleSelection: toggleResultSelection,
  toggleSelectPage: toggleSelectAllResults,
  ensureItemPage,
} = pagedSelection
const pagedResults = computed(() => pagedSelection.pagedItems.value)

const RESULT_DRAG_HOLD_MS = 260
type ResultDragSelection = {
  pointerId: number
  startId: string
  lastId: string
  targetSelected: boolean
  active: boolean
  visited: Set<string>
}

let resultDragSelection: ResultDragSelection | null = null
let resultDragTimer: ReturnType<typeof setTimeout> | null = null
let suppressNextResultClick = false

watch([search, sortBy, typeFilter, pageSize], () => {
  page.value = 1
})

watch(selecting, (value) => {
  if (!value) finishResultDragSelection()
})

function getFileName(path: string) {
  return path.split(/[/\\]/).pop() || path
}

function getGroupTitle(group: ResultGroup) {
  if (group.inputCount === 1) return getFileName(group.primary.input) || group.model
  return t('results.groupTitle', { count: group.inputCount, name: getFileName(group.primary.input) })
}

function resultCardId(item: Pick<ResultGroup, 'id'>) {
  return `result-card-${item.id}`
}

function openResultDir(group: ResultGroup) {
  task.revealPath(group.primary.outputs[0]?.path || group.output)
}

async function openInEditor(item: SeparationTask) {
  try {
    const project = await editor.ensureProjectForTask(item, { loadIntoSession: false })
    await editor.openProjectWindow(project.id)
  } catch (error) {
    message.error(error instanceof Error ? error.message : t('editor.notFound'))
  }
}

// 删除结果时回收当前结果对应的输出文件；分层输出的结果目录仅在已空时清理。
function trashEmptyDirs(item: SeparationTask) {
  return task.resultTrashEmptyDirs(item)
}

function groupTaskIds(group: ResultGroup) {
  return group.items.map(item => item.id)
}

function groupTaskIdTitle(group: ResultGroup) {
  return groupTaskIds(group).join('\n')
}

function removalItems(ids: string[]) {
  try { return task.getRemovalTasks(ids) }
  catch (error) { message.warning(String(error instanceof Error ? error.message : error)); return null }
}

async function removeResultRecords(items: SeparationTask[]) {
  if (!await cleanupLinkedEditorProjects(items)) return false
  try {
    await task.removeResults(items.map(item => item.id))
    return true
  } catch (error) {
    message.error(error instanceof Error ? error.message : t('results.removeRecordsFailed'))
    return false
  }
}

async function cleanupLinkedEditorProjects(items: SeparationTask[]) {
  try {
    const result = await editor.deleteProjectsForTasks(items.map(item => item.id))
    if (result.blockedProjectIds.length) {
      message.warning(t('results.removeEditorProjectsOpen', { count: result.blockedProjectIds.length }))
      return false
    }
    if (result.failedProjectIds.length) {
      message.error(t('results.removeEditorProjectsFailed', { count: result.failedProjectIds.length }))
      return false
    }
    return true
  } catch {
    message.error(t('results.removeEditorProjectsFailed', { count: items.length }))
    return false
  }
}

async function ensureLinkedEditorProjectsClosed(items: SeparationTask[]) {
  try {
    const openProjectIds = await editor.listOpenProjectsForTasks(items.map(item => item.id))
    if (!openProjectIds.length) return true
    message.warning(t('results.removeEditorProjectsOpen', { count: openProjectIds.length }))
  } catch {
    message.error(t('results.removeEditorProjectsFailed', { count: items.length }))
  }
  return false
}

async function refreshOrphanedEditorProjects(force = false) {
  if (orphanedProjectsLoading.value || (!force && orphanedProjectsCleaning.value)) return
  if (!task.initialized) {
    orphanedEditorProjects.value = []
    return
  }
  orphanedProjectsLoading.value = true
  try {
    orphanedEditorProjects.value = await editor.listOrphanedProjects(task.resultTasks.map(item => item.id))
  } catch {
    orphanedEditorProjects.value = []
  } finally {
    orphanedProjectsLoading.value = false
  }
}

function handleCleanupOrphanedEditorProjects() {
  const count = orphanedEditorProjects.value.length
  if (!count || orphanedProjectsCleaning.value) return
  const previewProjects = orphanedEditorProjects.value.slice(0, 6)
  dialog.warning({
    title: t('results.cleanupEditorProjectsTitle'),
    content: () => h('div', { style: 'display:grid;gap:10px;' }, [
      h('span', t('results.cleanupEditorProjectsContent', { count })),
      h('ul', { style: 'margin:0;padding-left:20px;color:var(--on-surface-muted);' }, previewProjects.map(project => (
        h('li', { key: project.projectId }, project.name)
      ))),
      count > previewProjects.length
        ? h('span', { style: 'color:var(--on-surface-muted);font-size:12px;' }, t('results.cleanupEditorProjectsMore', {
            count: count - previewProjects.length,
          }))
        : null,
    ]),
    positiveText: t('results.cleanupEditorProjectsAction'),
    negativeText: t('common.cancel'),
    positiveButtonProps: { type: 'error' },
    onPositiveClick: async () => {
      orphanedProjectsCleaning.value = true
      try {
        const result = await editor.deleteOrphanedProjects(task.resultTasks.map(item => item.id))
        if (result.blockedProjectIds.length) {
          message.warning(t('results.removeEditorProjectsOpen', { count: result.blockedProjectIds.length }))
          return false
        }
        if (result.failedProjectIds.length) {
          await refreshOrphanedEditorProjects(true)
          message.error(t('results.removeEditorProjectsFailed', { count: result.failedProjectIds.length }))
          return false
        }
        const removedCount = result.deletedProjectIds.length
        await refreshOrphanedEditorProjects(true)
        message.success(t('results.cleanupEditorProjectsSuccess', { count: removedCount }))
      } catch {
        message.error(t('results.cleanupEditorProjectsFailed'))
        return false
      } finally {
        orphanedProjectsCleaning.value = false
      }
    },
  })
}

function trashTargetsForItems(items: SeparationTask[]) {
  return task.resultTrashTargetsForTasks(items)
}

function trashEmptyDirsForItems(items: SeparationTask[]) {
  const seen = new Set<string>()
  return items.flatMap((item) => trashEmptyDirs(item)).filter((path) => {
    if (seen.has(path)) return false
    seen.add(path)
    return true
  })
}

type RemoveMessageMode = 'single' | 'multiple'

function confirmRemoveListOnly(failedCount: number, totalCount: number, mode: RemoveMessageMode) {
  return new Promise<boolean>((resolve) => {
    let settled = false
    const finish = (value: boolean) => {
      if (settled) return
      settled = true
      resolve(value)
    }
    dialog.warning({
      title: t('results.removeFilesConfirmTitle'),
      content: t(mode === 'single' ? 'results.removeFilesConfirmContentSingle' : 'results.removeFilesConfirmContentMultiple', {
        failed: failedCount,
        total: totalCount,
      }),
      positiveText: t('results.removeListOnlyAction'),
      negativeText: t('results.keepResultAction'),
      positiveButtonProps: { type: 'error' },
      negativeButtonProps: { secondary: true },
      onPositiveClick: () => finish(true),
      onNegativeClick: () => finish(false),
      onClose: () => finish(false),
    })
  })
}

function handleRemoveResult(group: ResultGroup) {
  const ids = groupTaskIds(group)
  const items = removalItems(ids)
  if (!items?.length) return
  const deleteFiles = ref(false)
  dialog.warning({
    title: t('results.removeTitle'),
    content: () => h('div', { style: 'display:grid;gap:12px;' }, [
      h('span', t('results.removeContent')),
      h('span', { class: 'result-remove__editor-hint' }, t('results.removeEditorProjectHint')),
      h(NCheckbox, {
        checked: deleteFiles.value,
        'onUpdate:checked': (value: boolean) => { deleteFiles.value = value },
      }, { default: () => t('results.removeDeleteFiles') }),
    ]),
    positiveText: t('results.removeAction'),
    negativeText: t('common.cancel'),
    positiveButtonProps: { type: 'error' },
    onPositiveClick: async () => {
      const finishRemove = async () => {
        return removeResultRecords(items)
      }
      if (!deleteFiles.value) {
        if (!await finishRemove()) return false
        message.success(t('results.removeSuccess'))
        return
      }
      if (!await ensureLinkedEditorProjectsClosed(items)) return false

      const { paths: targets, retained } = trashTargetsForItems(items)
      if (!targets.length) {
        if (!await finishRemove()) return false
        if (retained) message.success(t('results.removeFilesRetained', { count: retained }))
        else message.warning(t('results.removeNoFilesSingle'))
        return
      }

      try {
        const result = await task.trashResultPaths(targets, trashEmptyDirsForItems(items))
        if (!result.failed.length) {
          if (!await finishRemove()) return false
          if (retained) message.success(t('results.removeFilesRetained', { count: retained }))
          else message.success(t('results.removeFilesSuccessSingle'))
          return
        }

        const removeListOnly = await confirmRemoveListOnly(result.failed.length, targets.length, 'single')
        if (!removeListOnly) {
          message.warning(t('results.removeFilesKeptSingle'))
          return
        }
        if (!await finishRemove()) return false
        if (result.failed.length === targets.length) {
          message.warning(t('results.removeFilesAllFailedListOnly'))
        } else {
          message.warning(t('results.removeFilesPartialListOnly', { count: result.failed.length + retained }))
        }
      } catch (error) {
        const removeListOnly = await confirmRemoveListOnly(targets.length, targets.length, 'single')
        if (!removeListOnly) {
          message.error(error instanceof Error ? error.message : t('results.removeFilesFailed'))
          return
        }
        if (!await finishRemove()) return false
        message.warning(t('results.removeFilesAllFailedListOnly'))
      }
    },
  })
}

function handleRemoveSelected() {
  const ids = [...selectedResultIds.value]
  if (!ids.length) return
  const groups = resultGroups.value.filter((group) => ids.includes(group.id))
  const taskIds = groups.flatMap(groupTaskIds)
  const items = removalItems(taskIds)
  if (!items?.length) return
  const deleteFiles = ref(false)
  dialog.warning({
    title: t('results.removeSelectedTitle'),
    content: () => h('div', { style: 'display:grid;gap:12px;' }, [
      h('span', t('results.removeSelectedContent', { count: ids.length })),
      h('span', { class: 'result-remove__editor-hint' }, t('results.removeEditorProjectHint')),
      h(NCheckbox, {
        checked: deleteFiles.value,
        'onUpdate:checked': (value: boolean) => { deleteFiles.value = value },
      }, { default: () => t('results.removeDeleteFiles') }),
    ]),
    positiveText: t('results.removeSelectedPositive'),
    negativeText: t('common.cancel'),
    positiveButtonProps: { type: 'error' },
    onPositiveClick: async () => {
      const finishRemove = async (silent = false) => {
        if (!await removeResultRecords(items)) return false
        if (!silent) {
          message.success(t('results.removeSelectedSuccess', { count: ids.length }))
        }
        selectedResultIds.value = []
        selecting.value = false
        return true
      }

      if (!deleteFiles.value) {
        return await finishRemove() ? undefined : false
      }
      if (!await ensureLinkedEditorProjectsClosed(items)) return false

      const { paths: targets, retained } = trashTargetsForItems(items)
      if (!targets.length) {
        if (!await finishRemove(true)) return false
        if (retained) message.success(t('results.removeFilesRetained', { count: retained }))
        else message.warning(t('results.removeNoFilesMultiple'))
        return
      }

      try {
        const result = await task.trashResultPaths(targets, trashEmptyDirsForItems(items))
        if (!result.failed.length) {
          if (!await finishRemove(true)) return false
          if (retained) message.success(t('results.removeFilesRetained', { count: retained }))
          else message.success(t('results.removeFilesSuccessMultiple'))
          return
        }

        const removeListOnly = await confirmRemoveListOnly(result.failed.length, targets.length, 'multiple')
        if (!removeListOnly) {
          message.warning(t('results.removeFilesKeptMultiple'))
          return
        }
        if (!await finishRemove(true)) return false
        if (result.failed.length === targets.length) {
          message.warning(t('results.removeFilesAllFailedListOnly'))
        } else {
          message.warning(t('results.removeFilesPartialListOnly', { count: result.failed.length + retained }))
        }
      } catch (error) {
        const removeListOnly = await confirmRemoveListOnly(targets.length, targets.length, 'multiple')
        if (!removeListOnly) {
          message.error(error instanceof Error ? error.message : t('results.removeFilesFailed'))
          return
        }
        if (!await finishRemove(true)) return false
        message.warning(t('results.removeFilesAllFailedListOnly'))
      }
    },
  })
}

function handleClearResults() {
  const groups = [...resultGroups.value]
  if (!groups.length) return
  const items = removalItems(groups.flatMap(groupTaskIds))
  if (!items?.length) return
  const deleteFiles = ref(false)
  dialog.warning({
    title: t('results.clearTitle'),
    content: () => h('div', { style: 'display:grid;gap:12px;' }, [
      h('span', t('results.clearContent', { count: groups.length })),
      h('span', { class: 'result-remove__editor-hint' }, t('results.removeEditorProjectHint')),
      h(NCheckbox, {
        checked: deleteFiles.value,
        'onUpdate:checked': (value: boolean) => { deleteFiles.value = value },
      }, { default: () => t('results.removeDeleteFiles') }),
    ]),
    positiveText: t('results.clearPositive'),
    negativeText: t('common.cancel'),
    positiveButtonProps: { type: 'error' },
    onPositiveClick: async () => {
      const finishClear = async (silent = false) => {
        if (!await removeResultRecords(items)) return false
        if (!silent) {
          message.success(t('results.clearSuccess', { count: groups.length }))
        }
        selectedResultIds.value = []
        selecting.value = false
        return true
      }

      if (!deleteFiles.value) {
        return await finishClear() ? undefined : false
      }
      if (!await ensureLinkedEditorProjectsClosed(items)) return false

      const { paths: targets, retained } = trashTargetsForItems(items)
      if (!targets.length) {
        if (!await finishClear(true)) return false
        if (retained) message.success(t('results.removeFilesRetained', { count: retained }))
        else message.warning(t('results.removeNoFilesMultiple'))
        return
      }

      try {
        const result = await task.trashResultPaths(targets, trashEmptyDirsForItems(items))
        if (!result.failed.length) {
          if (!await finishClear(true)) return false
          if (retained) message.success(t('results.removeFilesRetained', { count: retained }))
          else message.success(t('results.removeFilesSuccessMultiple'))
          return
        }

        const removeListOnly = await confirmRemoveListOnly(result.failed.length, targets.length, 'multiple')
        if (!removeListOnly) {
          message.warning(t('results.removeFilesKeptMultiple'))
          return
        }
        if (!await finishClear(true)) return false
        if (result.failed.length === targets.length) {
          message.warning(t('results.removeFilesAllFailedListOnly'))
        } else {
          message.warning(t('results.removeFilesPartialListOnly', { count: result.failed.length + retained }))
        }
      } catch (error) {
        const removeListOnly = await confirmRemoveListOnly(targets.length, targets.length, 'multiple')
        if (!removeListOnly) {
          message.error(error instanceof Error ? error.message : t('results.removeFilesFailed'))
          return
        }
        if (!await finishClear(true)) return false
        message.warning(t('results.removeFilesAllFailedListOnly'))
      }
    },
  })
}

function setResultSelected(id: string, selected: boolean) {
  if (selectedResultSet.value.has(id) !== selected) toggleResultSelection(id)
}

function applyResultDragSelection(id: string) {
  const state = resultDragSelection
  if (!state?.active || state.visited.has(id)) return
  state.visited.add(id)
  setResultSelected(id, state.targetSelected)
}

function activateResultDragSelection() {
  const state = resultDragSelection
  if (!state || state.active) return
  state.active = true
  if (!selecting.value) selecting.value = true
  document.documentElement.classList.add('results-drag-selecting')
  applyResultDragSelection(state.startId)
  applyResultDragSelection(state.lastId)
}

function handleResultIconPointerDown(event: PointerEvent, id: string) {
  if (event.pointerType !== 'mouse' || event.button !== 0) return
  event.preventDefault()
  finishResultDragSelection()
  window.getSelection()?.removeAllRanges()
  resultDragSelection = {
    pointerId: event.pointerId,
    startId: id,
    lastId: id,
    targetSelected: selecting.value ? !selectedResultSet.value.has(id) : true,
    active: false,
    visited: new Set(),
  }
  resultDragTimer = setTimeout(activateResultDragSelection, RESULT_DRAG_HOLD_MS)
}

function handleResultIconPointerEnter(event: PointerEvent, id: string) {
  if (!resultDragSelection) return
  if ((event.buttons & 1) === 0) {
    finishResultDragSelection()
    return
  }
  resultDragSelection.lastId = id
  applyResultDragSelection(id)
}

function finishResultDragSelection(event?: PointerEvent) {
  const state = resultDragSelection
  if (!state || (event && event.pointerId !== state.pointerId)) return
  if (resultDragTimer) {
    clearTimeout(resultDragTimer)
    resultDragTimer = null
  }
  if (state.active) {
    suppressNextResultClick = true
    window.setTimeout(() => { suppressNextResultClick = false }, 0)
  }
  resultDragSelection = null
  document.documentElement.classList.remove('results-drag-selecting')
}

function hasSelectedResultText() {
  const selection = window.getSelection()
  return Boolean(selection && !selection.isCollapsed && selection.toString())
}

function handleResultRowClick(id: string) {
  if (suppressNextResultClick) {
    suppressNextResultClick = false
    return
  }
  if (hasSelectedResultText()) return
  if (selecting.value) toggleResultSelection(id)
  else toggleExpanded(id)
}

function handleResultsKeydown(event: KeyboardEvent) {
  if (event.defaultPrevented || event.key !== 'Escape' || !selecting.value) return
  if (document.querySelector('[role="dialog"], .n-modal-mask')) return
  event.preventDefault()
  finishResultDragSelection()
  toggleSelecting()
}

function handleResultsWindowBlur() {
  finishResultDragSelection()
}

function isExpanded(id: string) {
  return expandedIds.value.includes(id)
}

function toggleExpanded(id: string) {
  expandedIds.value = isExpanded(id)
    ? expandedIds.value.filter((item) => item !== id)
    : [...expandedIds.value, id]
}

function ensureExpanded(id: string) {
  if (!isExpanded(id)) {
    expandedIds.value = [...expandedIds.value, id]
  }
}

function shortenPath(value: string) {
  if (value.length <= 92) return value
  return `${value.slice(0, 34)}…${value.slice(-44)}`
}

async function copyNodeValue(value: string) {
  try {
    await navigator.clipboard.writeText(value)
    message.success(t('common.copied'))
  } catch {
    message.error(t('common.copyFailed'))
  }
}

function outputFileName(value: string) {
  return getFileName(value) || shortenPath(value)
}

function scrollToFocusedResult(id: string | null) {
  if (!id) return
  const group = resultGroups.value.find(group => group.id === id || group.items.some(item => item.id === id))
  const groupId = group?.id || id
  ensureExpanded(groupId)
  ensureItemPage(groupId)
  nextTick(() => {
    const target = document.getElementById(resultCardId({ id: groupId }))
    target?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  })
}

watch(() => task.focusedResultTaskId, (value) => {
  if (value) {
    scrollToFocusedResult(value)
    task.focusResultTask(null)
  }
})

onMounted(() => {
  window.addEventListener('keydown', handleResultsKeydown)
  window.addEventListener('pointerup', finishResultDragSelection)
  window.addEventListener('pointercancel', finishResultDragSelection)
  window.addEventListener('blur', handleResultsWindowBlur)
  void refreshOrphanedEditorProjects()
  if (task.focusedResultTaskId) {
    scrollToFocusedResult(task.focusedResultTaskId)
    task.focusResultTask(null)
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleResultsKeydown)
  window.removeEventListener('pointerup', finishResultDragSelection)
  window.removeEventListener('pointercancel', finishResultDragSelection)
  window.removeEventListener('blur', handleResultsWindowBlur)
  finishResultDragSelection()
})

function formatTime(value: number) {
  return new Date(value).toLocaleString()
}

function taskDurationMs(item: SeparationTask) {
  if (typeof item.durationMs === 'number' && Number.isFinite(item.durationMs) && item.durationMs >= 0) return item.durationMs
  if (item.startedAt && item.finishedAt) return Math.max(0, item.finishedAt - item.startedAt)
  return Math.max(0, item.updatedAt - item.createdAt)
}

function formatDurationMs(value: number | undefined) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return t('results.durationUnknown')
  return t('results.durationSeconds', { seconds: Math.max(0, Math.round(value / 1000)) })
}
</script>

<template>
  <div class="page results-page">
    <div class="page-header-compact results-page__header">
      <div>
        <h1>{{ t('results.title') }}</h1>
        <p>{{ t('results.subtitle') }}</p>
      </div>
      <div
        class="results-page__header-actions"
        :class="{ 'results-page__header-actions--selecting': selecting }"
      >
        <template v-if="selecting">
          <n-checkbox
            :checked="allResultsSelected"
            :indeterminate="someResultsSelected"
            @update:checked="toggleSelectAllResults"
          >
            {{ t('results.selectPage') }}
          </n-checkbox>
          <span class="results-page__selection-count" aria-live="polite">
            {{ t('results.selectedCount', { count: selectedResultIds.length }) }}
          </span>
          <n-button
            secondary
            type="error"
            :disabled="!selectedResultIds.length"
            @click="handleRemoveSelected"
          >
            <template #icon><n-icon :component="TrashOutline" /></template>
            {{ t('results.removeSelectedCount', { count: selectedResultIds.length }) }}
          </n-button>
          <n-button secondary @click="toggleSelecting">
            {{ t('results.batchExit') }}
          </n-button>
        </template>
        <template v-else>
          <n-button
            v-if="orphanedEditorProjects.length"
            secondary
            type="warning"
            :loading="orphanedProjectsCleaning"
            @click="handleCleanupOrphanedEditorProjects"
          >
            <template #icon><n-icon :component="TrashOutline" /></template>
            {{ t('results.cleanupEditorProjectsAction') }}
            <span class="results-page__cleanup-count">{{ orphanedEditorProjects.length }}</span>
          </n-button>
          <n-button v-if="task.resultTasks.length" secondary @click="toggleSelecting">
            {{ t('results.batchSelect') }}
          </n-button>
          <n-button
            v-if="task.resultTasks.length"
            secondary
            type="error"
            @click="handleClearResults"
          >
            <template #icon><n-icon :component="TrashOutline" /></template>
            {{ t('results.clearAction') }}
          </n-button>
        </template>
      </div>
    </div>

    <div v-if="task.resultTasks.length" class="results-toolbar">
      <n-input
        v-model:value="search"
        class="results-toolbar__search"
        clearable
        :placeholder="t('results.searchPlaceholder')"
      >
        <template #prefix><n-icon :component="SearchOutline" /></template>
      </n-input>

      <n-select v-model:value="typeFilter" class="results-toolbar__type" size="small" :options="typeOptions" :aria-label="t('results.typeFilter')" />
      <n-select
        v-model:value="sortBy"
        class="results-toolbar__sort"
        size="small"
        :options="sortOptions"
      >
        <template #arrow><n-icon :component="SwapVerticalOutline" /></template>
      </n-select>

      <span class="results-toolbar__count">{{ filteredResults.length }} / {{ resultGroups.length }}</span>
    </div>

    <div v-if="!task.resultTasks.length" class="results-empty" data-guide="results-empty">
      <n-icon :component="FolderOutline" size="46" />
      <strong>{{ t('results.empty') }}</strong>
      <span>{{ t('results.emptyHint') }}</span>
    </div>

    <div v-else-if="!filteredResults.length" class="results-empty" data-guide="results-empty">
      <n-icon :component="SearchOutline" size="42" />
      <strong>{{ t('results.noMatchTitle') }}</strong>
      <span>{{ t('results.noMatchHint') }}</span>
    </div>

    <div v-else class="results-list">
      <section
        v-for="item in pagedResults"
        :id="resultCardId(item)"
        :key="item.id"
        class="result-row" data-guide="result-item"
        :class="{ 'result-row--selected': selectedResultSet.has(item.id) }"
      >
        <button
          class="result-row__main"
          :class="{ 'result-row__main--selecting': selecting }"
          :aria-pressed="selecting ? selectedResultSet.has(item.id) : undefined"
          type="button"
          @click="handleResultRowClick(item.id)"
        >
          <span
            class="result-row__icon"
            @pointerdown="handleResultIconPointerDown($event, item.id)"
            @pointerenter="handleResultIconPointerEnter($event, item.id)"
          >
            <span
              v-if="selecting"
              class="result-row__selection-marker"
              :class="{ 'result-row__selection-marker--checked': selectedResultSet.has(item.id) }"
              aria-hidden="true"
            >
              <n-icon v-if="selectedResultSet.has(item.id)" :component="CheckmarkOutline" size="14" />
            </span>
            <n-icon v-else :component="DocumentTextOutline" size="18" />
          </span>

          <span class="result-row__body">
            <strong data-result-text>{{ getGroupTitle(item) }}</strong>
            <span class="result-row__meta">
              <span data-result-text class="result-row__type">{{ t(RESULT_TYPE_KEYS[item.resultType]) }}</span>
              <span data-result-text class="result-row__model" :title="groupTaskIdTitle(item)">{{ item.model }}</span>
              <span data-result-text>{{ item.inputCount }} {{ t('results.inputUnit') }}</span>
              <span data-result-text>{{ item.outputCount }} {{ t('results.stemUnit') }}</span>
              <span data-result-text>{{ formatDurationMs(item.durationMs) }}</span>
              <span data-result-text class="result-row__time"><n-icon :component="TimeOutline" /> {{ formatTime(item.updatedAt) }}</span>
            </span>
            <span data-result-text class="result-row__path">{{ shortenPath(item.output) }}</span>
          </span>

          <span
            v-if="!selecting"
            class="result-row__toggle"
            :class="{ 'result-row__toggle--open': isExpanded(item.id) }"
          >
            <n-icon :component="ChevronDownOutline" />
          </span>
        </button>

        <div v-if="!selecting" class="result-row__actions" data-guide="result-actions">
          <n-button v-if="item.inputCount === 1" size="small" type="primary" @click.stop="openInEditor(item.primary)">
            <template #icon><n-icon :component="ColorWandOutline" /></template>
            {{ t('results.openInEditor') }}
          </n-button>
          <n-button size="small" type="primary" secondary @click.stop="openResultDir(item)">
            <template #icon><n-icon :component="FolderOpenOutline" /></template>
            {{ t('results.openDirectory') }}
          </n-button>
          <n-button size="small" quaternary type="error" @click.stop="handleRemoveResult(item)">
            <template #icon><n-icon :component="TrashOutline" /></template>
            {{ t('results.removeAction') }}
          </n-button>
        </div>

        <n-collapse-transition class="result-row__collapse" :show="isExpanded(item.id)">
          <div class="result-row__details">
            <section
              v-for="result in item.items"
              :key="result.id"
              :class="['result-detail-card', { 'result-detail-card--compact': item.items.length === 1 }]"
            >
              <div v-if="item.items.length > 1" class="result-detail-card__head">
                <div>
                  <strong>{{ getFileName(result.input) }}</strong>
                  <span>{{ result.outputs.length }} {{ t('results.stemUnit') }} · {{ formatDurationMs(taskDurationMs(result)) }}</span>
                </div>
                <div class="result-detail-card__actions">
                  <n-button v-if="result.outputs.some(isAudioOutput)" size="tiny" secondary @click.stop="openInEditor(result)">
                    <template #icon><n-icon :component="ColorWandOutline" /></template>
                    {{ t('results.openInEditor') }}
                  </n-button>
                  <n-button size="tiny" tertiary @click.stop="task.revealPath(result.outputs[0]?.path || result.output)">
                    <template #icon><n-icon :component="FolderOpenOutline" /></template>
                    {{ t('results.openDirectory') }}
                  </n-button>
                </div>
              </div>
              <div class="result-detail-card__stems">
                <div v-for="output in result.outputs" :key="output.path" class="stem-line" :title="output.path">
                  <span class="stem-line__stem">{{ output.stem }}</span>
                  <span class="stem-line__path">{{ outputFileName(output.path) }}</span>
                </div>
              </div>
              <div v-if="result.nodeValues?.length" class="result-detail-card__node-values">
                <div class="node-value-line__header">{{ t('results.nodeValues') }}</div>
                <div
                  v-for="(entry, index) in result.nodeValues"
                  :key="`${entry.nodeId}-${entry.slot}-${index}`"
                  class="node-value-line"
                >
                  <span class="node-value-line__label">{{ entry.nodeType }}</span>
                  <span class="node-value-line__value">{{ entry.value }}</span>
                  <n-button size="tiny" tertiary @click.stop="copyNodeValue(entry.value)">
                    {{ t('common.copy') }}
                  </n-button>
                </div>
              </div>
            </section>
          </div>
        </n-collapse-transition>
      </section>
    </div>

    <div v-if="filteredResults.length" class="results-pagination">
      <n-pagination
        v-model:page="page"
        v-model:page-size="pageSize"
        :item-count="filteredResults.length"
        :page-sizes="pageSizeOptions"
        show-size-picker
      />
    </div>
  </div>
</template>

<style scoped>
.results-page {
  display: grid;
  gap: 12px;
}

.results-page__header {
  margin-bottom: 4px;
}

.results-page__header-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
}

.results-page__header-actions--selecting {
  padding: 8px 10px;
  border: 1px solid color-mix(in srgb, var(--primary-border) 52%, var(--outline));
  border-radius: 12px;
  background: color-mix(in srgb, var(--primary-softer) 42%, var(--surface-1));
}

.results-page__selection-count {
  min-width: 72px;
  color: var(--on-surface-muted);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.result-remove__editor-hint {
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.55;
}

.results-page__cleanup-count {
  min-width: 18px;
  padding: 0 5px;
  border-radius: 999px;
  background: color-mix(in srgb, currentColor 12%, transparent);
  font-size: 11px;
  line-height: 18px;
  text-align: center;
}

.results-toolbar {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 160px 190px auto;
  align-items: center;
  gap: 10px;
  padding: 10px;
  border: 1px solid color-mix(in srgb, var(--outline) 56%, transparent);
  border-radius: 14px;
  background:
    linear-gradient(180deg, rgba(255,255,255,0.025), transparent 52%),
    color-mix(in srgb, var(--surface-1) 72%, transparent);
}

.results-toolbar__count {
  min-width: 66px;
  text-align: right;
  font-size: 12px;
  color: var(--on-surface-muted);
}

.results-list {
  display: grid;
  gap: 8px;
}

.results-pagination {
  display: flex;
  justify-content: flex-end;
}

.result-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  padding: 10px 12px;
  border: 1px solid color-mix(in srgb, var(--outline) 54%, transparent);
  border-radius: 14px;
  background:
    linear-gradient(180deg, rgba(255,255,255,0.018), transparent 58%),
    color-mix(in srgb, var(--surface-1) 70%, transparent);
  transition: border-color 160ms ease, background 160ms ease;
}

.result-row--selected {
  border-color: color-mix(in srgb, var(--primary-border) 76%, transparent);
  background: color-mix(in srgb, var(--primary-softer) 48%, var(--surface-1));
}

.result-row:hover {
  border-color: color-mix(in srgb, var(--primary) 22%, var(--outline));
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--primary-soft) 10%, transparent), transparent 58%),
    color-mix(in srgb, var(--surface-1) 74%, transparent);
}

.result-row__type { padding: 1px 6px; border-radius: 5px; background: color-mix(in srgb, var(--primary-soft) 50%, transparent); color: var(--primary-strong); font-size: 10px; white-space: nowrap; }

.result-row--selected:hover {
  border-color: color-mix(in srgb, var(--primary-border) 88%, transparent);
  background: color-mix(in srgb, var(--primary-softer) 58%, var(--surface-1));
}

.result-row__main {
  min-width: 0;
  display: grid;
  grid-template-columns: 36px minmax(0, 1fr) 26px;
  align-items: center;
  gap: 12px;
  border: 0;
  padding: 0;
  color: inherit;
  background: transparent;
  text-align: left;
  cursor: pointer;
}

.result-row__main--selecting {
  grid-template-columns: 36px minmax(0, 1fr);
}

.result-row__icon {
  width: 36px;
  height: 36px;
  display: grid;
  place-items: center;
  border-radius: 11px;
  color: color-mix(in srgb, var(--primary-strong) 78%, var(--on-surface-muted));
  background: color-mix(in srgb, var(--primary-soft) 34%, var(--surface-2));
}

.result-row__selection-marker {
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  border: 1px solid color-mix(in srgb, var(--outline) 78%, transparent);
  border-radius: 999px;
  color: transparent;
  background: var(--surface-1);
  transition: border-color 140ms ease, color 140ms ease, background 140ms ease;
}

.result-row__selection-marker--checked {
  border-color: var(--primary-strong);
  color: var(--on-primary, #fff);
  background: var(--primary-strong);
}

.result-row__body {
  min-width: 0;
  display: grid;
  gap: 5px;
}

.result-row__body strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  line-height: 1.25;
}

[data-result-text] {
  cursor: text;
  user-select: text;
}

.result-row__path {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--on-surface-muted);
  font-size: 11px;
  font-family: inherit;
}

:global(html.results-drag-selecting),
:global(html.results-drag-selecting *) {
  cursor: crosshair !important;
  user-select: none !important;
}

.result-row__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 7px 10px;
  color: var(--on-surface-muted);
  font-size: 11px;
}

.result-row__time {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.result-row__toggle {
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  border-radius: 999px;
  color: var(--on-surface-muted);
  background: color-mix(in srgb, var(--surface-2) 68%, transparent);
  transition: transform 180ms ease, color 180ms ease, background 180ms ease;
}

.result-row__toggle--open {
  color: var(--primary-strong);
  background: var(--primary-soft);
  transform: rotate(180deg);
}

.result-row__actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}

.result-row__collapse {
  grid-column: 1 / -1;
  min-width: 0;
}

.result-row__details {
  display: grid;
  gap: 10px;
  padding: 12px 0 2px 54px;
}

.result-detail-card {
  display: grid;
  gap: 10px;
  padding: 10px;
  border: 1px solid color-mix(in srgb, var(--outline) 48%, transparent);
  border-radius: 13px;
  background: color-mix(in srgb, var(--surface) 28%, transparent);
}

.result-detail-card--compact {
  gap: 8px;
  max-width: 760px;
  padding: 0;
  border: 0;
  background: transparent;
}

.result-detail-card__head {
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.result-detail-card__head > div:first-child {
  min-width: 0;
  display: grid;
  gap: 3px;
}

.result-detail-card__head strong {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}

.result-detail-card__head span {
  color: var(--on-surface-muted);
  font-size: 12px;
}

.result-detail-card__actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.result-detail-card__stems {
  display: grid;
  gap: 8px;
}

.stem-line {
  display: grid;
  grid-template-columns: minmax(92px, 132px) minmax(0, 1fr);
  gap: 12px;
  align-items: center;
  padding: 8px 10px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--surface-2) 54%, transparent);
}

.stem-line span {
  font-size: 12px;
}

.stem-line__stem {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--primary-strong);
  font-weight: 700;
}

.node-value-line__header {
  color: var(--text-muted, #969eae);
  font-size: 12px;
  margin: 4px 0 2px;
}

.node-value-line {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 2px 0;
  font-size: 13px;
}

.node-value-line__label {
  color: var(--text-muted, #969eae);
  min-width: 120px;
}

.node-value-line__value {
  flex: 1;
  word-break: break-all;
}

.stem-line__path {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--on-surface-muted);
  font-size: 12px;
  font-family: inherit;
}

.results-empty {
  min-height: 260px;
  display: grid;
  place-items: center;
  align-content: center;
  gap: 10px;
  border: 1px dashed color-mix(in srgb, var(--outline) 64%, transparent);
  border-radius: 18px;
  color: var(--on-surface-muted);
  background: color-mix(in srgb, var(--surface-1) 58%, transparent);
}

.results-empty strong {
  color: var(--on-surface);
  font-size: 15px;
}

.results-empty span {
  font-size: 13px;
}

@media (max-width: 980px) {
  .results-toolbar {
    grid-template-columns: 1fr;
  }

  .results-toolbar__count {
    text-align: left;
  }

  .results-pagination {
    justify-content: flex-start;
  }

  .result-row {
    grid-template-columns: 1fr;
  }

  .result-row--selectable {
    grid-template-columns: auto minmax(0, 1fr);
  }

  .result-row__actions {
    justify-content: flex-start;
    padding-left: 54px;
  }

  .result-row__details {
    padding-left: 0;
  }
}

@media (max-width: 640px) {
  .results-page__header {
    align-items: flex-start;
    flex-direction: column;
  }

  .results-page__header-actions {
    justify-content: flex-start;
    width: 100%;
  }

  .result-row__main {
    grid-template-columns: 36px minmax(0, 1fr) 28px;
  }

  .result-row__icon {
    width: 36px;
    height: 36px;
    border-radius: 12px;
  }

  .result-row__actions {
    padding-left: 0;
  }

  .result-detail-card__head {
    align-items: flex-start;
    flex-direction: column;
  }

  .result-detail-card__actions {
    justify-content: flex-start;
  }

  .stem-line {
    grid-template-columns: 1fr;
    gap: 5px;
  }
}
</style>
