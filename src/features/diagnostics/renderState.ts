import { watch } from 'vue'
import { captureRenderDiagnostics, type RenderPreferences } from '@/utils/renderDiagnostics'
import { useDiagnosticWindowState } from './windowState'
import { createLogger } from '@/utils/logger'

const log = createLogger('ui.rendering')

export function useRenderDiagnosticState(preferences: () => RenderPreferences, enabled: () => boolean) {
  let changes: Array<RenderPreferences & { at: string }> = []
  let previous: RenderPreferences | undefined
  watch(() => [enabled(), ...Object.values(preferences())], () => {
    if (!enabled()) { changes = []; previous = undefined; return }
    const current = preferences()
    changes = [...changes, { ...current, routePath: current.routePath.split(/[?#]/)[0], at: new Date().toISOString() }].slice(-20)
    if (!previous || previous.theme !== current.theme || previous.resolvedTheme !== current.resolvedTheme || previous.animations !== current.animations) {
      log.info('Rendering preferences changed', { theme: current.theme, resolvedTheme: current.resolvedTheme, animations: current.animations,
        route: current.routePath.split(/[?#]/)[0], locale: current.locale, scale: current.scale })
    }
    previous = current
  }, { immediate: true, flush: 'post' })
  useDiagnosticWindowState('renderer', () => ({ rendering: captureRenderDiagnostics(preferences()), changes }))
}
