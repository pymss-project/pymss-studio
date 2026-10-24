import type { RouteRecordRaw } from 'vue-router'

export const appRoutes: RouteRecordRaw[] = [
  { path: '/', name: 'separate', component: () => import('@/views/SeparateView.vue') },
  { path: '/separate', redirect: { name: 'separate' } },
  { path: '/tasks', redirect: { name: 'results' } },
  { path: '/models', name: 'models', component: () => import('@/views/ModelsView.vue') },
  { path: '/workflows', name: 'workflows', component: () => import('@/views/WorkflowsView.vue') },
  { path: '/workflow-node-editor', name: 'workflow-node-editor', component: () => import('@/views/WorkflowNodeEditorView.vue') },
  { path: '/workflow-simple-editor', name: 'workflow-simple-editor', component: () => import('@/views/WorkflowSimpleEditorView.vue') },
  { path: '/results', name: 'results', component: () => import('@/views/ResultsView.vue') },
  { path: '/tools', name: 'tools', component: () => import('@/views/ToolsView.vue') },
  { path: '/plugins', name: 'plugins', component: () => import('@/views/PluginsView.vue') },
  { path: '/plugin-view/:plugin/:viewId', name: 'plugin-view', component: () => import('@/views/PluginWorkspaceView.vue') },
  { path: '/debug', name: 'debug', component: () => import('@/views/DebugView.vue') },
  { path: '/projects', redirect: { name: 'results' } },
  { path: '/editor', name: 'editor', component: () => import('@/views/EditorView.vue') },
  { path: '/settings', name: 'settings', component: () => import('@/views/SettingsView.vue') },
]
