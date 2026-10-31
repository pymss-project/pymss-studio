import type { DefineLocaleMessage } from 'vue-i18n'

const files = import.meta.glob<DefineLocaleMessage>('./locales/*.json', { eager: true, import: 'default' })
export const messages = Object.fromEntries(Object.entries(files).map(([path, data]) => [
  path.slice('./locales/'.length, -'.json'.length), data,
]))

export function resolveLocale(requested: string): string {
  const locales = Object.keys(messages)
  const normalized = requested.toLowerCase()
  const language = normalized.split('-')[0]
  return locales.find(locale => locale.toLowerCase() === normalized)
    || locales.find(locale => locale.toLowerCase() === language)
    || locales.find(locale => locale.toLowerCase().split('-')[0] === language)
    || 'en'
}
