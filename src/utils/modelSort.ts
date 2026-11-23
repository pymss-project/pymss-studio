type SortableModel = { name: string; sortKey?: string | null }
type ModelWithPublication = SortableModel & { author?: string | null; publishDate?: string | null }
export type ModelPublicationSort = 'author' | 'publish-date-desc' | 'publish-date-asc'

export function compareModelDefaultOrder(a: SortableModel, b: SortableModel, compare: (a: string, b: string) => number) {
  const key = (model: SortableModel) => model.sortKey?.trim() || model.name.toLowerCase()
  return compare(key(a), key(b)) || compare(a.name, b.name) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0)
}

export function compareModelPublicationOrder(
  a: ModelWithPublication,
  b: ModelWithPublication,
  sort: ModelPublicationSort,
  compare: (a: string, b: string) => number,
) {
  const field = sort === 'author' ? 'author' : 'publishDate'
  const aValue = a[field]?.trim() || ''
  const bValue = b[field]?.trim() || ''
  if (!aValue && bValue) return 1
  if (aValue && !bValue) return -1

  // Catalog dates use YYYY-MM-DD, so their string order is chronological.
  const order = compare(aValue, bValue)
  return (sort === 'publish-date-desc' ? -order : order) || compareModelDefaultOrder(a, b, compare)
}
