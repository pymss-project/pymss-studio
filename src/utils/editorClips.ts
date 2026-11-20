import type { EditorClip, EditorSource, EditorTrack } from '@/types/editor'

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

export function syncSingleClipFadeCompatibility(track: EditorTrack) {
  if (track.clips?.length !== 1) return
  const clip = track.clips[0]
  const duration = Math.max(0, Number(clip.duration || 0))
  clip.fadeIn = Math.min(duration, Math.max(0, Number(clip.fadeIn || 0)))
  clip.fadeOut = Math.min(duration, Math.max(0, Number(clip.fadeOut || 0)))
  track.fadeIn = clip.fadeIn
  track.fadeOut = clip.fadeOut
}

export function overwriteClipsInRange(
  clips: EditorClip[],
  start: number,
  duration: number,
  createClipId: () => string,
) {
  const rangeStart = Math.max(0, Number(start || 0))
  const rangeEnd = rangeStart + Math.max(0, Number(duration || 0))
  if (rangeEnd - rangeStart < 0.01) return clips.map((clip) => ({ ...clip }))

  const result: EditorClip[] = []
  for (const clip of clips) {
    const clipStart = Math.max(0, Number(clip.start || 0))
    const clipEnd = clipStart + Math.max(0, Number(clip.duration || 0))
    if (clipEnd <= rangeStart || clipStart >= rangeEnd) {
      result.push({ ...clip })
      continue
    }

    const leftDuration = Math.max(0, rangeStart - clipStart)
    const rightDuration = Math.max(0, clipEnd - rangeEnd)
    if (leftDuration >= 0.01) {
      result.push({
        ...clip,
        duration: leftDuration,
        fadeIn: Math.min(clip.fadeIn, leftDuration),
        fadeOut: 0,
      })
    }
    if (rightDuration >= 0.01) {
      // Recording boundaries follow the sample clock. Millisecond rounding
      // can extend the remaining slice beyond its original source samples.
      const offset = clip.offset + Math.max(0, rangeEnd - clipStart)
      const duration = Math.min(rightDuration, Math.max(0, clip.offset + clip.duration - offset))
      result.push({
        ...clip,
        id: leftDuration >= 0.01 ? createClipId() : clip.id,
        start: rangeEnd,
        offset,
        duration,
        fadeIn: 0,
        fadeOut: Math.min(clip.fadeOut, duration),
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

const TIMELINE_EPSILON = 1e-8

export function adjacentClipEdge(edges: readonly number[], time: number, direction: -1 | 1) {
  for (let step = 0; step < edges.length; step++) {
    const edge = edges[direction === 1 ? step : edges.length - 1 - step]
    if ((edge - time) * direction > TIMELINE_EPSILON) return edge
  }
  return null
}

export type ClipTimingEditMode = 'move' | 'trim-start' | 'trim-end'
type ClipTiming = Pick<EditorClip, 'start' | 'offset' | 'duration'>

export function resolveClipTimingEdit(
  clip: ClipTiming,
  mode: ClipTimingEditMode,
  delta: number,
  sourceDuration = 0,
): Partial<ClipTiming> {
  if (mode === 'move') return { start: Math.max(0, clip.start + delta) }

  const minDuration = Math.min(0.05, clip.duration)
  if (mode === 'trim-start') {
    const minDelta = -Math.min(clip.start, clip.offset)
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
  return {
    duration: Math.min(maxDuration, Math.max(minDuration, clip.duration + delta)),
  }
}

export function resolveSnappedClipTimingEdit(
  clip: ClipTiming,
  mode: ClipTimingEditMode,
  delta: number,
  sourceDuration: number,
  snapTargets: readonly number[],
  tolerance: number,
) {
  let patch = resolveClipTimingEdit(clip, mode, delta, sourceDuration)
  let snapTime: number | null = null
  let nearestDistance = tolerance
  const start = clip.start
  const end = start + clip.duration
  const edges = mode === 'move' ? [start, end] : [mode === 'trim-start' ? start : end]
  for (const edge of edges) {
    for (const target of snapTargets) {
      const distance = Math.abs(edge + delta - target)
      if (distance >= nearestDistance) continue
      const snapped = resolveClipTimingEdit(clip, mode, target - edge, sourceDuration)
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
