<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useTaskStore } from '@/stores/task'
import { useModelStore } from '@/stores/model'
import { useToastStore } from '@/stores/toast'
import { isMainTaskOwner } from '@/utils/taskOwner'
import { activityChanges, downloadAnnouncementState, taskAnnouncementState, type ActivityChange } from '@/utils/activityAnnouncements'

const { t } = useI18n()
const task = useTaskStore()
const model = useModelStore()
const toast = useToastStore()
const announcement = ref('')
const revision = ref(0)
const pending = new Map<string, ActivityChange>()
const knownTaskStates = new WeakMap<object, string | null>()
let timer: ReturnType<typeof setTimeout> | undefined

function statusText(change: ActivityChange) {
  if (change.kind === 'download') {
    const keys = { running: 'models.downloadStatusDownloading', done: 'models.downloaded', failed: 'models.cardDownloadError', cancelled: 'models.downloadStatusCancelled', paused: 'models.downloadStatusPaused', interrupted: 'models.downloadInterrupted' }
    return t(keys[change.state])
  }
  const keys = { running: 'accessibility.processing', done: 'tasks.statusDone', failed: 'tasks.statusFailed', cancelled: 'tasks.statusCancelled', paused: 'models.downloadStatusPaused', interrupted: 'models.downloadInterrupted' }
  return t(keys[change.state])
}

function enqueue(changes: ActivityChange[]) {
  for (const change of changes) pending.set(`${change.kind}:${change.id}`, change)
  if (!pending.size || timer) return
  timer = setTimeout(() => {
    timer = undefined
    const groups = new Map<string, ActivityChange[]>()
    for (const change of pending.values()) {
      const key = `${change.kind}:${change.state}`
      const group = groups.get(key) || []
      group.push(change)
      groups.set(key, group)
    }
    pending.clear()
    announcement.value = [...groups.values()].map(group => {
      const first = group[0]!
      const key = first.kind === 'task'
        ? group.length === 1 ? 'accessibility.taskStatus' : 'accessibility.tasksStatus'
        : group.length === 1 ? 'accessibility.downloadStatus' : 'accessibility.downloadsStatus'
      return t(key, { name: first.name, count: group.length, status: statusText(first) })
    }).join(t('accessibility.announcementSeparator'))
    revision.value++
  }, 700)
}

// Hydration establishes a baseline; opening a window must not read old task history.
watch(() => ({ ready: task.initialized, items: task.tasks.map(item => ({ record: item, id: item.id, name: item.input.split(/[\\/]/).pop() || item.model, state: taskAnnouncementState(item.status) })) }), (current, previous) => {
  // A failed record deletion restores the same task objects. Keep weak state
  // references so that rollback does not announce old results as new work.
  for (const item of previous?.items || []) {
    if (!knownTaskStates.has(item.record)) knownTaskStates.set(item.record, item.state)
  }
  const changed = current.items.filter(item => knownTaskStates.get(item.record) !== item.state)
  if (isMainTaskOwner() && current.ready && previous?.ready) enqueue(activityChanges(previous.items, changed, 'task'))
  for (const item of current.items) knownTaskStates.set(item.record, item.state)
})
watch(() => ({ ready: model.initialized, states: { ...model.downloadStates }, items: Object.values(model.downloadTasks).map(item => ({ id: item.taskId, name: item.model, state: downloadAnnouncementState(item.status) })) }), (current, previous) => {
  if (!isMainTaskOwner() || !current.ready || !previous?.ready) return
  const names = new Set(current.items.map(item => item.name))
  // Successful downloads are removed from the task map once marked downloaded.
  const completed = previous.items.filter(item => !names.has(item.name) && current.states[item.name] === 'done')
    .map(item => ({ ...item, state: 'done' as const }))
  enqueue(activityChanges(previous.items, [...current.items, ...completed], 'download'))
})
onBeforeUnmount(() => { if (timer) clearTimeout(timer) })
</script>

<template>
  <div role="status" aria-live="polite" aria-relevant="additions text" aria-atomic="false" data-app-announcements>
    <div id="app-message-target" />
    <span :key="revision" class="app-announcement-text">{{ announcement }}</span>
    <span v-for="item in toast.toasts" :key="item.id" class="app-announcement-text">{{ item.message }}</span>
  </div>
</template>

<style scoped>
.app-announcement-text { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
</style>
