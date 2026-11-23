import { invoke } from '@tauri-apps/api/core'
import i18n from '@/i18n'

export function describeTaskReminder(mode: 'model' | 'workflow', { done, failed, cancelled }: {
  done: number
  failed: number
  cancelled: number
}) {
  const { t } = i18n.global
  const kind = mode === 'workflow' ? 'workflow' : 'separation'
  const outcome = failed ? (done ? 'partialFailure' : 'failed') : (cancelled ? 'finished' : 'completed')
  const title = t(`settings.taskNotification.${kind}.${outcome}`)

  if (!failed && !cancelled) {
    return { title, body: t('settings.taskNotification.allCompleted', done) }
  }
  if (failed && !done && !cancelled) {
    return { title, body: t('settings.taskNotification.allFailed', failed) }
  }

  const counts: string[] = []
  if (done) counts.push(t('settings.taskNotification.completedCount', { count: done }))
  if (failed) counts.push(t('settings.taskNotification.failedCount', { count: failed }))
  if (cancelled) counts.push(t('settings.taskNotification.cancelledCount', { count: cancelled }))
  const results = counts.join(t('settings.taskNotification.separator'))
  const body = failed
    ? t('settings.taskNotification.summaryWithFailure', { results })
    : t('settings.taskNotification.summary', { results })
  return { title, body }
}

export function showTaskNotification(title: string, body: string) {
  return invoke<void>('show_task_notification', { title, body })
}

export function prepareTaskNotifications() {
  return invoke<void>('prepare_task_notifications')
}
