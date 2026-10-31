import { reactive } from 'vue'
import type { GlobalThemeOverrides } from 'naive-ui'

export const presentation = reactive({ mode: 'light', locale: 'en', primary: '#258c7b', fontFamily: 'system-ui, sans-serif', componentTheme: null as GlobalThemeOverrides | null })
