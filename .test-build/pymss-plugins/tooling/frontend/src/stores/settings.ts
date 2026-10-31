import { reactive } from 'vue'

const settings = reactive({ outputDir: '', modelDir: '', platform: '', machine: '' })
export function useSettingsStore() { return settings }
