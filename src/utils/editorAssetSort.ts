import type { EditorSource } from '@/types/editor'

export type EditorAssetSort = 'added' | 'name' | 'duration'

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })

/** Returns a sorted copy; "added" keeps the project order. */
export function sortEditorSources<T extends Pick<EditorSource, 'name' | 'duration'>>(sources: readonly T[], order: EditorAssetSort): T[] {
  if (order === 'name') return [...sources].sort((a, b) => collator.compare(a.name, b.name))
  if (order === 'duration') return [...sources].sort((a, b) => (b.duration || 0) - (a.duration || 0))
  return [...sources]
}
