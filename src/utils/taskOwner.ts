import { getCurrentWindow } from '@tauri-apps/api/window'
import { isWebRuntime } from '@/utils/webRuntime'

export function isMainTaskOwner(): boolean {
  if (isWebRuntime()) return true
  if (typeof window === 'undefined' || !('__TAURI_INTERNALS__' in window)) return true
  try { return getCurrentWindow().label === 'main' } catch { return false }
}
