import { readSettings, writeSettings } from '../sdk'

export type AppStoreName = 'audio-tools'
export function isTauriRuntime() { return false }
export async function loadAppStore<T>(_name: AppStoreName): Promise<T | null> {
  return await readSettings() as T
}
export async function saveAppStore(_name: AppStoreName, value: unknown) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new TypeError('Invalid tool settings')
  await writeSettings(value as Record<string, unknown>)
}
