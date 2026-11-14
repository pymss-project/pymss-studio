type Track = { index: number; language: string }
type TracksByPath = Record<string, Track[]>
type Selection = Record<string, number[]>

/** Languages tagged across two or more multi-track files, with their batch checkbox state. Untagged tracks stay per-file. */
export function trackLanguageStates(paths: string[], tracksByPath: TracksByPath, selection: Selection) {
  const files = paths.filter(path => tracksByPath[path])
  if (files.length < 2) return []
  const languages = [...new Set(files.flatMap(path => tracksByPath[path].map(track => track.language)))]
    .filter(language => language && language !== 'und')
  return languages.map((language) => {
    const states = files.flatMap(path => tracksByPath[path]
      .filter(track => track.language === language)
      .map(track => selection[path]?.includes(track.index) ?? false))
    const selected = states.filter(Boolean).length
    return { language, checked: selected === states.length, indeterminate: selected > 0 && selected < states.length }
  })
}

/** Selects or clears every track of one language in every file, keeping each file's other choices and track order. */
export function selectTrackLanguage(paths: string[], tracksByPath: TracksByPath, selection: Selection, language: string, selected: boolean) {
  const next = { ...selection }
  for (const path of paths) {
    const tracks = tracksByPath[path]
    const matching = tracks?.filter(track => track.language === language).map(track => track.index) || []
    if (!tracks || !matching.length) continue
    const current = new Set(next[path] || [])
    matching.forEach(index => selected ? current.add(index) : current.delete(index))
    next[path] = tracks.map(track => track.index).filter(index => current.has(index))
  }
  return next
}
