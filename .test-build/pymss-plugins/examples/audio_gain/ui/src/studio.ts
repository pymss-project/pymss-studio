import { reactive } from 'vue'
import type { GlobalThemeOverrides } from 'naive-ui'
import { resolveLocale } from './i18n'

export const OPERATION_ID = 'example.audio_gain.apply'
export const LAYOUT_REFRESH_EVENT = 'pymss:layout-refresh'
export type Progress = { id: number; fraction?: number; message?: string }
export type RpcPromise<T> = Promise<T> & { readonly id: number }
export type OperationResult = { operation: string; outputs: { audio?: { kind: string; path?: string }; report?: { value?: Record<string, unknown> } } }
type Theme = { locale?: string; colorMode?: 'light' | 'dark'; componentTheme?: GlobalThemeOverrides }
type StudioSDK = {
  ready(): Promise<void>
  onTheme(handler: (theme: Theme) => void): void
  appInfo(): Promise<{ sdkVersion: number; capabilities: string[]; locale: string; defaultOutputDir?: string }>
  readSettings(): Promise<Record<string, unknown>>
  writeSettings(data: Record<string, unknown>): Promise<unknown>
  pickAudio(): Promise<{ path: string } | null>
  pickDirectory(): Promise<{ path: string } | null>
  revealPath(path: string): Promise<unknown>
  startOperation(operation: string, params: Record<string, unknown>, inputs: Record<string, unknown>, progress: (update: Progress) => void, options: { outputDir: string }): RpcPromise<OperationResult>
  cancelOperation(id: number): Promise<boolean>
}

export const normalizeLocale = resolveLocale
export const host = reactive({ ready: false, locale: normalizeLocale(navigator.language), mode: 'light' as 'light' | 'dark', theme: null as GlobalThemeOverrides | null })
let receivedLocale = false

export function sdk(): StudioSDK {
  const value = (window as unknown as { pymss?: StudioSDK }).pymss
  if (!value || window.parent === window || !['ready', 'appInfo', 'onTheme', 'startOperation', 'cancelOperation', 'readSettings', 'writeSettings', 'pickAudio', 'pickDirectory', 'revealPath'].every(key => typeof value[key as keyof StudioSDK] === 'function')) {
    throw new Error('STUDIO_SDK_UNAVAILABLE')
  }
  return value
}

export async function bounded<T>(promise: Promise<T>): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([promise, new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('STUDIO_REQUEST_TIMEOUT')), 10000) })])
  } finally { clearTimeout(timer) }
}

export async function connect() {
  const client = sdk()
  client.onTheme(theme => {
    if (theme.locale) { host.locale = normalizeLocale(theme.locale); receivedLocale = true }
    if (theme.colorMode === 'light' || theme.colorMode === 'dark') host.mode = theme.colorMode
    if (theme.componentTheme) host.theme = theme.componentTheme
  })
  await bounded(client.ready())
  const info = await bounded(client.appInfo())
  if (info.sdkVersion !== __STUDIO_REQUIREMENTS__.sdkVersion) {
    throw new Error(`This plugin requires Studio SDK ${__STUDIO_REQUIREMENTS__.sdkVersion}; the host provides ${info.sdkVersion ?? 'no SDK version'}.`)
  }
  const capabilities = new Set(info.capabilities)
  const missing = __STUDIO_REQUIREMENTS__.requiredCapabilities.filter(capability => !capabilities.has(capability))
  if (missing.length) throw new Error(`Studio is missing required plugin capabilities: ${missing.join(', ')}`)
  if (!receivedLocale) host.locale = normalizeLocale(info.locale || 'en')
  const settings = await bounded(client.readSettings())
  host.ready = true
  return { settings, outputDir: info.defaultOutputDir || '' }
}

export function overlay(open: boolean) {
  window.parent.postMessage({ jsonrpc: '2.0', method: 'pymss.overlay', params: { open } }, '*')
  if (!open) window.dispatchEvent(new Event(LAYOUT_REFRESH_EVENT))
}
