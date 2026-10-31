export function sameAudioDirectory(first: string, second: string): boolean {
  if (!first.trim() || !second.trim()) return false
  const normalize = (value: string) => {
    const trimmed = value.trim()
    const windows = /^(?:[a-z]:[\\/]|\\\\|\/\/)/i.test(trimmed)
    const path = (windows ? trimmed.replace(/\\/g, '/') : trimmed).replace(/\/\.(?=\/|$)/g, '').replace(/\/+$/, '') || '/'
    return windows ? path.toLowerCase() : path
  }
  return normalize(first) === normalize(second)
}
