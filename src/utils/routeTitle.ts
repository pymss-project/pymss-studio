export const APP_TITLE = 'Pymss Studio'

/**
 * Returns the `nav.*` key that names a route. Workflow editors and plugin workspaces
 * belong to their library section; `null` means the route has no section of its own.
 */
export function routeNavKey(name: unknown): string | null {
  if (typeof name !== 'string' || !name) return null
  if (name.startsWith('workflow-')) return 'workflows'
  if (name === 'plugin-view') return 'plugins'
  return name
}

/** Window title announced by screen readers when the page changes. */
export function documentTitle(pageTitle: string): string {
  return pageTitle ? `${pageTitle} - ${APP_TITLE}` : APP_TITLE
}
