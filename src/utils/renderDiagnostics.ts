export type RenderPreferences = { theme: string; resolvedTheme?: string; animations: boolean; scale: number; locale: string; resolvedLocale?: string; routePath: string }
export function captureRenderDiagnostics(preferences: RenderPreferences) {
  const root = document.documentElement
  const shell = document.querySelector('.app-shell')
  const style = getComputedStyle(root)
  const dark = root.classList.contains('dark-theme'), light = root.classList.contains('light-theme')
  const pages = [...document.querySelectorAll('.page')]
  const modals = [...document.querySelectorAll('.n-modal-container')]
  const visible = (element: Element) => {
    const rect = element.getBoundingClientRect(), css = getComputedStyle(element)
    return rect.width > 0 && rect.height > 0 && css.display !== 'none' && css.visibility !== 'hidden'
  }
  return {
    checkedAt: new Date().toISOString(),
    configuredTheme: preferences.theme, configuredAnimations: preferences.animations,
    resolvedTheme: preferences.resolvedTheme || null,
    configuredScale: preferences.scale, configuredLocale: preferences.locale,
    resolvedLocale: preferences.resolvedLocale || null,
    routePath: preferences.routePath.split(/[?#]/)[0],
    userAgent: navigator.userAgent, platform: navigator.platform, language: navigator.language,
    documentLanguage: root.lang, visibility: document.visibilityState,
    themeClass: dark && light ? 'mixed' : dark ? 'dark' : light ? 'light' : 'missing',
    animationsDisabled: shell ? shell.classList.contains('no-animations') : null,
    devicePixelRatio: window.devicePixelRatio, viewportWidth: window.innerWidth, viewportHeight: window.innerHeight,
    prefersDark: window.matchMedia('(prefers-color-scheme: dark)').matches,
    prefersReducedMotion: window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    surfaceColor: style.getPropertyValue('--surface-1').trim(), textColor: style.getPropertyValue('--on-surface').trim(),
    pageRoots: pages.length, visiblePageRoots: pages.slice(0, 32).filter(visible).length,
    modalRoots: modals.length, visibleModalRoots: modals.slice(0, 32).filter(visible).length, sampleLimit: 32,
    viewTransitionsAvailable: typeof (document as Document & { startViewTransition?: unknown }).startViewTransition === 'function',
  }
}
export type RenderDiagnosticState = ReturnType<typeof captureRenderDiagnostics>
