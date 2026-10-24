import { createRouter, createWebHashHistory } from 'vue-router'
import { appRoutes } from './routes'
import { invoke } from '@tauri-apps/api/core'
import { createWorkflowEditorRouteGuard } from '@/workflows/editorWindow'
import { isMainTaskOwner } from '@/utils/taskOwner'
import { isTauriRuntime } from '@/utils/appStore'
import { recordFeatureVisit } from '@/utils/logger'
import { useToastStore } from '@/stores/toast'
import { useWorkflowStore } from '@/stores/workflow'
import i18n from '@/i18n'

const router = createRouter({
  history: createWebHashHistory(),
  routes: appRoutes,
})
router.beforeEach(createWorkflowEditorRouteGuard({
  isDesktopMainWindow: () => isTauriRuntime() && isMainTaskOwner(),
  openWindow: async (command, args) => {
    const result = await invoke(command, args)
    const workflow = useWorkflowStore()
    const id = args.payload.newWorkflow ? '__new__' : args.payload.workflowId
    workflow.recordEditorOpened(result, command === 'open_workflow_simple_editor_window' ? 'simple' : 'advanced', id)
    return result
  },
  onError: error => useToastStore().error(error instanceof Error ? error.message : i18n.global.t('workflows.editorWindowOpenFailed')),
}))
router.afterEach((to, from, failure) => {
  if (!failure && typeof to.name === 'string' && to.name !== from.name) recordFeatureVisit(to.name)
})

export default router
