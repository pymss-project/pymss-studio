import { reactive } from 'vue'

export function createDescriptionDraft(options: {
  read: () => string
  write: (text: string) => Promise<string>
  limit: number
  delay?: number
}) {
  const initial = options.read()
  const state = reactive({ text: initial, savedText: initial, saving: false, error: '', unconfirmed: false })
  let timer: ReturnType<typeof setTimeout> | undefined
  let pending: Promise<boolean> | null = null
  let version = 0
  const dirty = () => state.unconfirmed || state.text.trim() !== state.savedText
  const tooLong = () => state.text.length > options.limit
  function cancelTimer() { clearTimeout(timer); timer = undefined }
  function synchronize() {
    const current = options.read()
    if (!dirty() && !state.saving) state.text = state.savedText = current
    else if (!state.unconfirmed && !state.saving && state.text.trim() === current) { state.savedText = current; state.error = '' }
  }
  async function drain() {
    while (dirty()) {
      if (tooLong()) return false
      const text = state.text, revision = version
      if (!state.unconfirmed && text.trim() === options.read()) { state.savedText = text.trim(); state.error = ''; continue }
      state.error = ''
      state.saving = true
      try {
        const saved = await options.write(text)
        state.unconfirmed = false
        state.savedText = saved
        if (version === revision) state.text = saved
        state.error = ''
      } catch (error) {
        state.unconfirmed = true
        state.error = error instanceof Error ? error.message : String(error)
        return false
      } finally { state.saving = false }
    }
    return true
  }
  function flush(): Promise<boolean> {
    cancelTimer()
    if (pending) return pending
    pending = drain().finally(() => { pending = null })
    return pending
  }
  function update(text: string) {
    state.text = text
    state.error = ''
    version++
    cancelTimer()
    if (dirty() && !tooLong()) timer = setTimeout(() => { void flush() }, options.delay ?? 800)
  }
  function discard() {
    cancelTimer()
    version++
    state.unconfirmed = false
    state.text = state.savedText = options.read()
    state.error = ''
  }
  return { state, dirty, tooLong, update, synchronize, flush, discard, cancelTimer }
}
