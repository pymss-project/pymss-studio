type SortableModel = { name: string; sortKey?: string | null }

export function compareModelDefaultOrder(a: SortableModel, b: SortableModel, compare: (a: string, b: string) => number) {
  const key = (model: SortableModel) => model.sortKey?.trim() || model.name.toLowerCase()
  return compare(key(a), key(b)) || compare(a.name, b.name) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0)
}
