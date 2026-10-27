import { invoke } from '@tauri-apps/api/core'
import { isTutorialFrame, tutorialAdapter } from '../features/onboarding/demoContext.ts'
import { isWebRuntime } from './webRuntime.ts'

export type AppStoreName = 'app-settings' | 'task-history' | 'model-state' | 'editor-ui' | 'audio-tools' | 'workflow-state' | 'separate-state' | 'update-state'

/** True when a native desktop IPC bridge exists (Tauri webview or the web-mode emulation). */
export const hasBackendBridge = () => isWebRuntime() || (typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window)
export const isTauriRuntime = () => typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
export const isDesktopRuntime = isTauriRuntime

const saveQueues = new Map<AppStoreName, Promise<void>>()

function localStoreKey(name: AppStoreName) {
  return `pymss-studio:${name}`
}

function errorMessage(error: unknown) {
  if (typeof error === 'string') return error
  if (error instanceof Error) return error.message
  if (error && typeof error === 'object' && 'message' in error) {
    const message = (error as { message?: unknown }).message
    if (typeof message === 'string') return message
  }
  return String(error)
}

function isMissingUpdateStore(error: unknown, name: AppStoreName) {
  return name === 'update-state' && /unknown app store:\s*update-state/i.test(errorMessage(error))
}

function readLocalStore<T>(name: AppStoreName): T | null {
  const raw = localStorage.getItem(localStoreKey(name))
  if (!raw) return null
  try {
    const value = JSON.parse(raw) as T
    return value && typeof value === 'object' ? value : null
  } catch {
    return null
  }
}

export async function loadAppStore<T>(name: AppStoreName): Promise<T | null> {
  if (isTutorialFrame()) return tutorialAdapter().invoke('load_app_store', { name }) as Promise<T | null>
  if (!hasBackendBridge()) {
    return readLocalStore<T>(name)
  }
  try {
    const value = await invoke<T | null>('load_app_store', { name })
    return value && typeof value === 'object' ? value : null
  } catch (error) {
    // 0.0.13 did not register update-state in the Rust store map. Keep the
    // update flow usable when a newer frontend is paired with that backend.
    if (isMissingUpdateStore(error, name)) return readLocalStore<T>(name)
    throw error
  }
}

async function writeAppStore(name: AppStoreName, data: unknown, serialized: string) {
  if (isTutorialFrame()) { await tutorialAdapter().invoke('save_app_store', { name, data }); return }
  if (!hasBackendBridge()) {
    localStorage.setItem(localStoreKey(name), serialized)
    return
  }
  try {
    await invoke('save_app_store', { name, data })
  } catch (error) {
    // Persist update preferences locally when upgrading from a backend that
    // predates the update-state app store. Other stores still surface errors.
    if (!isMissingUpdateStore(error, name)) throw error
    localStorage.setItem(localStoreKey(name), serialized)
  }
}

export function saveAppStore(name: AppStoreName, data: unknown) {
  let serialized: string | undefined
  let snapshot: unknown
  try {
    serialized = JSON.stringify(data)
    if (serialized === undefined) {
      throw new TypeError(`App store "${name}" cannot persist an undefined value`)
    }
    snapshot = JSON.parse(serialized) as unknown
  } catch (error) {
    return Promise.reject(error)
  }
  const previous = saveQueues.get(name) ?? Promise.resolve()
  const save = previous
    .catch(() => undefined)
    .then(() => writeAppStore(name, snapshot, serialized))
  saveQueues.set(name, save)
  void save.finally(() => {
    if (saveQueues.get(name) === save) saveQueues.delete(name)
  }).catch(() => undefined)
  return save
}
