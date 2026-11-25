export type MountedTool<Entry> = {
  id: string
  entry: Entry
  pluginsRoot: string
  busy: boolean
  usedAt: number
}

/**
 * Keep recently used tool pages mounted. Idle instances whose tool changed or disappeared are
 * dropped; busy instances always stay until their task settles. Beyond `limit`, the least
 * recently used idle instance other than the shown one is evicted.
 */
export function reconcileMountedTools<Entry>(
  instances: readonly MountedTool<Entry>[],
  currentIds: ReadonlySet<string>,
  shown: { id: string; entry: Entry; pluginsRoot: string } | null,
  limit: number,
  usedAt: number,
): MountedTool<Entry>[] {
  const next = instances.filter(instance => instance.busy || currentIds.has(instance.id))
  if (shown) {
    const existing = next.find(instance => instance.id === shown.id)
    if (existing) existing.usedAt = usedAt
    else next.push({ ...shown, busy: false, usedAt })
  }
  while (next.length > limit) {
    const evictable = next.filter(instance => !instance.busy && instance.id !== shown?.id)
    if (!evictable.length) break
    const oldest = evictable.reduce((left, right) => left.usedAt <= right.usedAt ? left : right)
    next.splice(next.indexOf(oldest), 1)
  }
  return next
}
