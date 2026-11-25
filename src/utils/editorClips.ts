import type { EditorClip, EditorSource, EditorTrack } from '@/types/editor'

const TIMELINE_EPSILON = 1e-8

export function clipColor(clip: EditorClip, track: EditorTrack) {
  return clip.color || track.color || '#7aa2ff'
}

export function clipsForTrack(track: EditorTrack, sourceMap: Map<string, EditorSource>): EditorClip[] {
  if (track.clips?.length) return track.clips
  const source = sourceMap.get(track.sourceId)
  if (!source || source.duration <= 0) return []
  return [{
    id: `clip_${track.id}`,
    assetId: source.id,
    start: 0,
    offset: 0,
    duration: source.duration,
    volume: 1,
    fadeIn: track.fadeIn,
    fadeOut: track.fadeOut,
    muted: false,
    locked: false,
  }]
}

export function clipIsActive(clip: EditorClip, timelineTime: number) {
  return timelineTime >= clip.start && timelineTime < clip.start + clip.duration
}

export function clipMediaTime(clip: EditorClip, timelineTime: number) {
  return clip.offset + Math.max(0, timelineTime - clip.start)
}

export function timelineDuration(tracks: EditorTrack[], sourceMap: Map<string, EditorSource>) {
  let duration = 0
  for (const track of tracks) {
    for (const clip of clipsForTrack(track, sourceMap)) {
      duration = Math.max(duration, clip.start + clip.duration)
    }
  }
  return duration
}

/**
 * Keep clips inside their source after the source file became shorter. Clips that start past
 * the new end are removed. Returns whether any clip changed.
 */
export function fitClipsToSource(tracks: EditorTrack[], source: Pick<EditorSource, 'id' | 'duration'>) {
  const length = Math.max(0, Number(source.duration || 0))
  let changed = false
  for (const track of tracks) {
    if (!track.clips?.some(clip => clip.assetId === source.id)) continue
    const clips: EditorClip[] = []
    for (const clip of track.clips) {
      if (clip.assetId !== source.id) {
        clips.push(clip)
        continue
      }
      const offset = Math.min(Math.max(0, clip.offset), length)
      const duration = Math.min(clip.duration, length - offset)
      if (offset === clip.offset && duration === clip.duration) {
        clips.push(clip)
        continue
      }
      changed = true
      if (duration <= TIMELINE_EPSILON) continue
      clips.push({ ...clip, offset, duration, fadeIn: Math.min(clip.fadeIn, duration), fadeOut: Math.min(clip.fadeOut, duration) })
    }
    track.clips = clips
    syncSingleClipFadeCompatibility(track)
  }
  return changed
}

export function syncSingleClipFadeCompatibility(track: EditorTrack) {
  if (track.clips?.length !== 1) return
  const clip = track.clips[0]
  const duration = Math.max(0, Number(clip.duration || 0))
  clip.fadeIn = Math.min(duration, Math.max(0, Number(clip.fadeIn || 0)))
  clip.fadeOut = Math.min(duration, Math.max(0, Number(clip.fadeOut || 0)))
  track.fadeIn = clip.fadeIn
  track.fadeOut = clip.fadeOut
}

export function hasLockedClipInRange(clips: readonly EditorClip[], start: number, duration: number) {
  return clips.some(clip => clip.locked && (
    Math.min(start + duration, clip.start + clip.duration) - Math.max(start, clip.start) > TIMELINE_EPSILON
  ))
}

export function overwriteClipsInRange(
  clips: EditorClip[],
  start: number,
  duration: number,
  createClipId: () => string,
  minimumDuration = 0.01,
) {
  const rangeStart = Math.max(0, Number(start || 0))
  const rangeEnd = rangeStart + Math.max(0, Number(duration || 0))
  const minimum = Math.max(TIMELINE_EPSILON, minimumDuration)
  if (rangeEnd - rangeStart < minimum) return clips.map((clip) => ({ ...clip }))

  const result: EditorClip[] = []
  for (const clip of clips) {
    const clipStart = Math.max(0, Number(clip.start || 0))
    const clipEnd = clipStart + Math.max(0, Number(clip.duration || 0))
    if (Math.min(clipEnd, rangeEnd) - Math.max(clipStart, rangeStart) <= TIMELINE_EPSILON) {
      result.push({ ...clip })
      continue
    }

    const leftDuration = Math.max(0, rangeStart - clipStart)
    if (leftDuration >= minimum) {
      result.push({
        ...clip,
        duration: leftDuration,
        fadeIn: Math.min(clip.fadeIn, leftDuration),
        fadeOut: 0,
      })
    }
    // Keep the remaining slice within the original source bounds.
    const offset = clip.offset + Math.max(0, rangeEnd - clipStart)
    const rightDuration = Math.min(clipEnd - rangeEnd, clip.offset + clip.duration - offset)
    if (rightDuration >= minimum) {
      result.push({
        ...clip,
        id: leftDuration >= minimum ? createClipId() : clip.id,
        start: rangeEnd,
        offset,
        duration: rightDuration,
        fadeIn: 0,
        fadeOut: Math.min(clip.fadeOut, rightDuration),
      })
    }
  }
  return result.sort((left, right) => left.start - right.start)
}

export function timelineClipEdges(tracks: EditorTrack[], sources: Map<string, EditorSource>, excludeClipId?: string) {
  const edges = new Set([0])
  for (const track of tracks) {
    for (const clip of clipsForTrack(track, sources)) {
      if (clip.id === excludeClipId) continue
      if (Number.isFinite(clip.start)) edges.add(clip.start)
      const end = clip.start + clip.duration
      if (Number.isFinite(end)) edges.add(end)
    }
  }
  return [...edges].sort((a, b) => a - b)
}

export function overlappingClipRanges(clips: readonly Pick<EditorClip, 'start' | 'duration'>[]) {
  const ranges: { start: number; end: number }[] = []
  if (clips.length < 2) return ranges

  let furthestEnd = 0
  for (const clip of [...clips].sort((left, right) => left.start - right.start)) {
    const clipEnd = clip.start + clip.duration
    const overlapEnd = Math.min(furthestEnd, clipEnd)
    if (overlapEnd - clip.start > TIMELINE_EPSILON) {
      const previous = ranges[ranges.length - 1]
      if (previous && clip.start - previous.end <= TIMELINE_EPSILON) {
        previous.end = Math.max(previous.end, overlapEnd)
      } else {
        ranges.push({ start: clip.start, end: overlapEnd })
      }
    }
    furthestEnd = Math.max(furthestEnd, clipEnd)
  }
  return ranges
}

export function adjacentClipEdge(edges: readonly number[], time: number, direction: -1 | 1) {
  for (let step = 0; step < edges.length; step++) {
    const edge = edges[direction === 1 ? step : edges.length - 1 - step]
    if ((edge - time) * direction > TIMELINE_EPSILON) return edge
  }
  return null
}

export function resolveSnappedSplitTime(
  clip: Pick<EditorClip, 'start' | 'duration'>,
  time: number,
  minSliceDuration: number,
  targets: readonly number[],
  tolerance: number,
) {
  const earliest = clip.start + minSliceDuration
  const latest = clip.start + clip.duration - minSliceDuration
  let result = time
  let nearestDistance = tolerance
  for (const target of targets) {
    if (target < earliest || target > latest) continue
    const distance = Math.abs(target - time)
    if (distance < nearestDistance) {
      result = target
      nearestDistance = distance
    }
  }
  return result
}

export type ClipTimingEditMode = 'move' | 'trim-start' | 'trim-end'
type ClipTiming = Pick<EditorClip, 'start' | 'offset' | 'duration'>
export type ClipTrimBounds = { start: number; end: number }
type ClipInterval = Pick<EditorClip, 'id' | 'start' | 'duration'>

export function clipTrimBounds(clip: ClipInterval, clips: readonly ClipInterval[]): ClipTrimBounds {
  const end = clip.start + clip.duration
  let earliest = 0
  let latest = Infinity

  for (const other of clips) {
    if (other.id === clip.id || other.duration <= 0) continue
    const otherEnd = other.start + other.duration
    // Keep existing overlaps from growing when extending either edge.
    if (other.start < clip.start - TIMELINE_EPSILON) {
      earliest = Math.max(earliest, Math.min(clip.start, otherEnd))
    }
    if (otherEnd > end + TIMELINE_EPSILON) {
      latest = Math.min(latest, Math.max(end, other.start))
    }
  }
  return { start: earliest, end: latest }
}

export function resolveClipTimingEdit(
  clip: ClipTiming,
  mode: ClipTimingEditMode,
  delta: number,
  sourceDuration = 0,
  trimBounds?: ClipTrimBounds,
): Partial<ClipTiming> {
  if (mode === 'move') return { start: Math.max(0, clip.start + delta) }

  const minDuration = Math.min(0.05, clip.duration)
  if (mode === 'trim-start') {
    const minDelta = Math.max(-clip.offset, (trimBounds?.start ?? 0) - clip.start)
    const maxDelta = clip.duration - minDuration
    const applied = Math.min(maxDelta, Math.max(minDelta, delta))
    return {
      start: clip.start + applied,
      offset: clip.offset + applied,
      duration: clip.duration - applied,
    }
  }
  const maxDuration = sourceDuration > 0
    ? Math.max(minDuration, sourceDuration - clip.offset)
    : Number.MAX_SAFE_INTEGER
  const maxTrimDuration = (trimBounds?.end ?? Infinity) - clip.start
  return { duration: Math.min(maxTrimDuration, maxDuration, Math.max(minDuration, clip.duration + delta)) }
}

export function resolveSnappedClipTimingEdit(
  clip: ClipTiming,
  mode: ClipTimingEditMode,
  delta: number,
  sourceDuration: number,
  snapTargets: readonly number[],
  tolerance: number,
  trimBounds?: ClipTrimBounds,
) {
  let patch = resolveClipTimingEdit(clip, mode, delta, sourceDuration, trimBounds)
  let snapTime: number | null = null
  let nearestDistance = tolerance
  const start = clip.start
  const end = start + clip.duration
  const edges = mode === 'move' ? [start, end] : [mode === 'trim-start' ? start : end]
  for (const edge of edges) {
    for (const target of snapTargets) {
      const distance = Math.abs(edge + delta - target)
      if (distance >= nearestDistance) continue
      const snapped = resolveClipTimingEdit(clip, mode, target - edge, sourceDuration, trimBounds)
      const snappedStart = snapped.start ?? start
      const snappedEdge = edge === start && mode !== 'trim-end'
        ? snappedStart
        : snappedStart + (snapped.duration ?? clip.duration)
      if (Math.abs(snappedEdge - target) > TIMELINE_EPSILON) continue
      patch = snapped
      snapTime = target
      nearestDistance = distance
    }
  }
  return { patch, snapTime }
}
