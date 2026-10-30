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

export type ClipTimingEditMode = 'move' | 'trim-start' | 'trim-end'

function roundTime(value: number) {
  return Math.round(value * 1000) / 1000
}

export function resolveClipTimingEdit(
  clip: Pick<EditorClip, 'start' | 'offset' | 'duration'>,
  mode: ClipTimingEditMode,
  delta: number,
  sourceDuration = 0,
): Partial<Pick<EditorClip, 'start' | 'offset' | 'duration'>> {
  if (mode === 'move') {
    return { start: roundTime(Math.max(0, clip.start + delta)) }
  }
  if (mode === 'trim-start') {
    const minDelta = -Math.min(clip.start, clip.offset)
    const maxDelta = Math.max(0, clip.duration - 0.05)
    const applied = Math.min(maxDelta, Math.max(minDelta, delta))
    return {
      start: roundTime(clip.start + applied),
      offset: roundTime(clip.offset + applied),
      duration: roundTime(clip.duration - applied),
    }
  }
  const maxDuration = sourceDuration > 0
    ? Math.max(0.05, sourceDuration - clip.offset)
    : Number.MAX_SAFE_INTEGER
  return {
    duration: roundTime(Math.min(maxDuration, Math.max(0.05, clip.duration + delta))),
  }
}
