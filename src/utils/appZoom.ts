import { emit, listen, type UnlistenFn } from '@tauri-apps/api/event'
import { getCurrentWebview } from '@tauri-apps/api/webview'

export const DEFAULT_SCALE_FACTOR = 1
export const SCALE_FACTOR_MIN = 0.75
export const SCALE_FACTOR_MAX = 1.5
export const SCALE_FACTOR_STEP = 0.05

export function normalizeScaleFactor(value: unknown, fallback = DEFAULT_SCALE_FACTOR) {
  const normalized = Number(value)
  if (!Number.isFinite(normalized)) return fallback
  return Math.min(SCALE_FACTOR_MAX, Math.max(SCALE_FACTOR_MIN, normalized))
}

export async function applyScaleFactor(value: number) {
  if (typeof window !== 'undefined' && !('__TAURI_INTERNALS__' in window)) return
  await getCurrentWebview().setZoom(normalizeScaleFactor(value))
}

// Every window applies the stored factor when it opens; this event carries later changes to
// windows that are already open. Receivers apply the zoom only and never persist it.
const SCALE_FACTOR_EVENT = 'pymss://scale-factor-changed'

type ScaleFactorChange = { value: number; source: string }

export async function broadcastScaleFactor(value: number) {
  if (typeof window !== 'undefined' && !('__TAURI_INTERNALS__' in window)) return
  await emit(SCALE_FACTOR_EVENT, { value: normalizeScaleFactor(value), source: getCurrentWebview().label } satisfies ScaleFactorChange)
}

export function listenForScaleFactor(apply: (value: number) => void): Promise<UnlistenFn> {
  const label = getCurrentWebview().label
  return listen<ScaleFactorChange>(SCALE_FACTOR_EVENT, ({ payload }) => {
    if (payload?.source !== label) apply(normalizeScaleFactor(payload?.value))
  })
}
