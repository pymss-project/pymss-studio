import { effectScope, onBeforeUnmount, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useWorkflowStore } from '@/stores/workflow'
import { createDescriptionDraft } from '@/workflows/descriptionDraft'
import { resolveWorkflowDescription, WORKFLOW_NOTES_MAX_LENGTH } from '@/workflows/metadata'
import { registerWindowCloseGuard, WindowClosePersistenceError } from '@/utils/windowCloseGuards'

type Session = { model: ReturnType<typeof createDescriptionDraft>; users: number; release: () => void }
const sessions = new WeakMap<ReturnType<typeof useWorkflowStore>, Map<string, Session>>()

export function useWorkflowDescriptionDraft(id: string) {
  const store = useWorkflowStore()
  const { t } = useI18n()
  let drafts = sessions.get(store)
  if (!drafts) { drafts = new Map(); sessions.set(store, drafts) }
  let session = drafts.get(id)
  if (!session) {
    const source = () => store.workflows.find(item => item.id === id)
    const model = createDescriptionDraft({
      read: () => resolveWorkflowDescription(source()),
      write: async text => resolveWorkflowDescription(await store.updateWorkflowNotes(id, text)),
      limit: WORKFLOW_NOTES_MAX_LENGTH,
    })
    const scope = effectScope(true)
    const cleanup = () => {
      if (entry.users || model.dirty()) return
      model.cancelTimer()
      scope.stop()
      unregister()
      drafts!.delete(id)
    }
    const unregister = registerWindowCloseGuard(async () => {
      if (!await model.flush()) throw new WindowClosePersistenceError(
        new Error(model.state.error || t('workflows.descriptionDraftRetained')),
        async () => { model.discard(); cleanup() },
      )
      cleanup()
    }, 50)
    const entry: Session = { model, users: 0, release() {
      entry.users--
      if (entry.users) return
      if (!model.state.saving && (model.state.error || model.tooLong())) { cleanup(); return }
      void model.flush().then(cleanup)
    } }
    scope.run(() => watch([() => Boolean(source()), () => resolveWorkflowDescription(source())], ([exists]) => {
      if (!exists) { model.discard(); cleanup(); return }
      model.synchronize()
      cleanup()
    }))
    session = entry
    drafts.set(id, session)
  }
  session.users++
  session.model.synchronize()
  onBeforeUnmount(session.release)
  return session.model
}
