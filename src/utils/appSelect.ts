import type { VNodeChild } from 'vue'

export type AppSelectScalar = string | number
export type AppSelectValue = AppSelectScalar | AppSelectScalar[] | null | undefined
export type AppSelectOption = {
  value?: AppSelectScalar
  label?: string | number | ((option: AppSelectOption, selected: boolean) => VNodeChild)
  disabled?: boolean
  type?: string
  key?: AppSelectScalar
  children?: readonly AppSelectOption[]
}
export type AppSelectEntry = {
  key: string
  value: AppSelectScalar
  label: string
  disabled: boolean
  group: string
  raw: AppSelectOption
}

// Encode values instead of coercing them: numeric 0, string "0", and "" are distinct.
export function appSelectKey(value: AppSelectScalar) {
  return `${typeof value === 'number' ? 'n' : 's'}:${value}`
}

export function appSelectEntries(options: readonly AppSelectOption[]): AppSelectEntry[] {
  const result: AppSelectEntry[] = []
  const seen = new Set<string>()
  function visit(items: readonly AppSelectOption[], group = '', disabled = false) {
    for (const item of items) {
      const label = typeof item.label === 'function' ? String(item.value ?? '') : String(item.label ?? item.value ?? '')
      if (Array.isArray(item.children)) {
        visit(item.children, label, disabled || item.disabled === true)
      } else if (typeof item.value === 'string' || (typeof item.value === 'number' && Number.isFinite(item.value))) {
        const key = appSelectKey(item.value)
        if (seen.has(key)) continue
        seen.add(key)
        result.push({ key, value: item.value, label, group, disabled: disabled || item.disabled === true, raw: item })
      }
    }
  }
  visit(options)
  return result
}

export function filterAppSelectEntries(entries: readonly AppSelectEntry[], query: string) {
  const needle = query.trim().toLocaleLowerCase()
  return needle ? entries.filter(entry => entry.label.toLocaleLowerCase().includes(needle)) : [...entries]
}
