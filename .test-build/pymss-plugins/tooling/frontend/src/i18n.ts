import messages from '@tool/locales'

export { messages }

export function resolveLocale(requested: string): string {
  const locales = Object.keys(messages)
  const normalized = requested.toLowerCase()
  const language = normalized.split('-')[0]
  return locales.find(locale => locale.toLowerCase() === normalized)
    || locales.find(locale => locale.toLowerCase() === language)
    || locales.find(locale => locale.toLowerCase().split('-')[0] === language)
    || 'en'
}
