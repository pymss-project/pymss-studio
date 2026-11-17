export type ActivityState = 'running' | 'done' | 'failed' | 'cancelled' | 'paused' | 'interrupted'
export type ActivitySnapshot = { id: string; name: string; state: ActivityState | null }
export type ActivityChange = ActivitySnapshot & { kind: 'task' | 'download'; state: ActivityState }

export function taskAnnouncementState(status: string): ActivityState | null {
  if (status === 'queued') return null
  if (status === 'done' || status === 'failed' || status === 'cancelled') return status
  return ['preparing', 'validating_input', 'downloading_model', 'ensuring_model', 'loading_model', 'separating', 'writing_output'].includes(status) ? 'running' : null
}

export function downloadAnnouncementState(status: string): ActivityState | null {
  if (status === 'idle') return null
  if (status === 'preparing' || status === 'downloading') return 'running'
  if (status === 'error') return 'failed'
  if (['done', 'cancelled', 'paused', 'interrupted'].includes(status)) return status as ActivityState
  return null
}

export function activityChanges(previous: readonly ActivitySnapshot[], current: readonly ActivitySnapshot[], kind: ActivityChange['kind']): ActivityChange[] {
  const states = new Map(previous.map(item => [item.id, item.state]))
  return current.flatMap(item => item.state && item.state !== states.get(item.id) ? [{ ...item, state: item.state, kind }] : [])
}
