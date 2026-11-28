// Detailed waveform peaks for zoomed-in timelines. The overview peaks of an asset are too coarse
// beyond a few dozen pixels per second, so visible stretches load fixed blocks of finer peaks.
import { invoke } from '@tauri-apps/api/core'

/** Must match DETAIL_BLOCK_SECONDS and DETAIL_POINTS_PER_SECOND in python/worker_audio.py. */
export const DETAIL_BLOCK_SECONDS = 4
export const DETAIL_POINTS_PER_SECOND = 512
const MAX_BLOCKS_PER_REQUEST = 64
/** About 6 MB of stereo peaks. */
const MAX_CACHED_BLOCKS = 600

/** Peaks of one block, per channel. */
export type DetailBlock = number[][]
export type DetailBlockLookup = (index: number) => DetailBlock | null | undefined

const cache = new Map<string, DetailBlock>()
const failed = new Set<string>()
const inflight = new Map<string, Promise<void>>()
const generations = new Map<string, number>()

const blockKey = (path: string, index: number) => `${index}\u0000${path}`

function remember(key: string, block: DetailBlock) {
  cache.delete(key)
  cache.set(key, block)
  while (cache.size > MAX_CACHED_BLOCKS) cache.delete(cache.keys().next().value as string)
}

/** Loaded block, `null` when it could not be measured, `undefined` while unknown. */
export function detailBlock(path: string, index: number): DetailBlock | null | undefined {
  const key = blockKey(path, index)
  return failed.has(key) ? null : cache.get(key)
}

/** Blocks covering a stretch of an asset, in seconds. */
export function detailBlockRange(start: number, end: number) {
  const first = Math.max(0, Math.floor(start / DETAIL_BLOCK_SECONDS))
  const last = Math.max(first, Math.floor(Math.max(start, end - 1e-9) / DETAIL_BLOCK_SECONDS))
  return Array.from({ length: last - first + 1 }, (_, offset) => first + offset)
}

/** Loads the blocks not yet known for `path`; resolves once every requested block is settled. */
export function requestDetailBlocks(path: string, indexes: readonly number[]): Promise<void> {
  const wanted = [...new Set(indexes)].filter(index => Number.isInteger(index) && index >= 0)
  const waiting = wanted.map(index => inflight.get(blockKey(path, index))).filter(Boolean) as Promise<void>[]
  const missing = wanted
    .filter(index => detailBlock(path, index) === undefined && !inflight.has(blockKey(path, index)))
    .slice(0, MAX_BLOCKS_PER_REQUEST)
  if (!missing.length) return Promise.all(waiting).then(() => undefined)

  const generation = generations.get(path) ?? 0
  const task = invoke<{ blocks?: Record<string, DetailBlock> }>('generate_waveform_peaks', {
    payload: { path, detailBlocks: missing },
  }).then((result) => {
    if ((generations.get(path) ?? 0) !== generation) return
    for (const index of missing) {
      const block = result.blocks?.[String(index)]
      // Blocks past the end of the file come back absent; they hold no audio to draw.
      remember(blockKey(path, index), Array.isArray(block) ? block : [])
    }
  }).catch(() => {
    if ((generations.get(path) ?? 0) !== generation) return
    for (const index of missing) failed.add(blockKey(path, index))
  }).finally(() => {
    for (const index of missing) inflight.delete(blockKey(path, index))
  })
  for (const index of missing) inflight.set(blockKey(path, index), task)
  return Promise.all([task, ...waiting]).then(() => undefined)
}

/** Forgets the blocks of a file that was replaced on disk. */
export function invalidateWaveformDetail(path: string) {
  generations.set(path, (generations.get(path) ?? 0) + 1)
  const suffix = `\u0000${path}`
  for (const key of [...cache.keys()]) if (key.endsWith(suffix)) cache.delete(key)
  for (const key of [...failed]) if (key.endsWith(suffix)) failed.delete(key)
}

/**
 * Largest detailed peak over `[start, end)` seconds of an asset. `channel` selects one channel or,
 * with `'all'`, the loudest of them. Returns `null` when a block it needs is not available, so the
 * caller can fall back to the overview peaks.
 */
export function detailPeak(lookup: DetailBlockLookup, channel: number | 'all', start: number, end: number) {
  let peak = 0
  for (const index of detailBlockRange(start, end)) {
    const block = lookup(index)
    if (!block) return null
    const blockStart = index * DETAIL_BLOCK_SECONDS
    const channels = channel === 'all' ? block : [block[channel] ?? block[0] ?? []]
    for (const values of channels) {
      if (!values?.length) continue
      const from = Math.max(0, Math.floor((start - blockStart) * DETAIL_POINTS_PER_SECOND))
      const to = Math.min(values.length, Math.ceil((end - blockStart) * DETAIL_POINTS_PER_SECOND))
      for (let point = from; point < Math.max(from + 1, to) && point < values.length; point += 1) {
        if (values[point] > peak) peak = values[point]
      }
    }
  }
  return peak
}
