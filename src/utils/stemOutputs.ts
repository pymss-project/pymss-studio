export type StemOutput = { stem: string; path: string; nodeId?: string; kind?: 'file'; mediaType?: string }

export function isAudioOutput(output: StemOutput) {
  return output.kind !== 'file' || output.mediaType?.startsWith('audio/') === true
}

function fileName(path: string, windows: boolean) {
  return String(path || '').split(windows ? /[/\\]/ : /[/]/).pop() || ''
}

function stripExtension(value: string) {
  return value.replace(/\.[^/.]+$/, '')
}

/**
 * Resolve the display stem used by the result and separation pages.
 *
 * Graph SaveAudio nodes only return file paths, and their default names may
 * include the input basename (for example `song_vocals.wav`). Strip that
 * stable prefix when it is present while preserving custom workflow names.
 */
export function stemFromOutputPath(path: string, inputPath?: string, windows = /^[A-Za-z]:|^\\\\/.test(path) || /^[A-Za-z]:|^\\\\/.test(inputPath || '')) {
  let stem = stripExtension(fileName(path, windows)).trim()
  const inputStem = stripExtension(fileName(inputPath || '', windows)).trim()
  const prefix = inputStem ? `${inputStem}_` : ''
  if (prefix && stem.toLowerCase().startsWith(prefix.toLowerCase())) {
    stem = stem.slice(prefix.length).trim()
  }
  return stem || 'output'
}

/**
 * Normalize output metadata from both worker generations. Older workflow
 * workers returned `{ path, name }` without the `stem` field used by the
 * single-separation result contract.
 */
export function normalizeStemOutputs(value: unknown, inputPath?: string, windows?: boolean): StemOutput[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return []
    const raw = item as Record<string, unknown>
    const path = String(raw.path || '')
    if (!path) return []
    const explicitStem = String(raw.stem || '').trim()
    const nameHint = String(raw.name || '').trim()
    return [{
      stem: explicitStem || stemFromOutputPath(nameHint || path, inputPath, windows ?? (/^[A-Za-z]:|^\\\\/.test(path) || /^[A-Za-z]:|^\\\\/.test(inputPath || ''))),
      path,
      ...(typeof raw.nodeId === 'string' ? { nodeId: raw.nodeId } : {}),
      ...(raw.kind === 'file' ? { kind: 'file' as const, ...(typeof raw.mediaType === 'string' ? { mediaType: raw.mediaType } : {}) } : {}),
    }]
  })
}
