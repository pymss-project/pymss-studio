import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import {
  adjacentClipEdge,
  clipIsActive,
  clipMediaTime,
  clipTrimBounds,
  clipsForTrack,
  fitClipsToSource,
  overlappingClipRanges,
  overwriteClipsInRange,
  resolveClipTimingEdit,
  resolveSnappedClipTimingEdit,
  resolveSnappedSplitTime,
  syncSingleClipFadeCompatibility,
  timelineClipEdges,
  timelineDuration,
} from '../src/utils/editorClips.ts'
import type { EditorClip, EditorSource, EditorTrack } from '../src/types/editor.ts'

const source: EditorSource = {
  id: 'source-a',
  role: 'recording',
  path: 'recording.wav',
  name: 'recording.wav',
  duration: 4,
  sampleRate: 48_000,
  channels: 1,
}

const track: EditorTrack = {
  id: 'track-a',
  sourceId: source.id,
  role: 'recording',
  name: 'Recording',
  volume: 1,
  pan: 0,
  muted: false,
  solo: false,
  fadeIn: 0,
  fadeOut: 0,
}

describe('editor clip timeline helpers', () => {
  it('migrates a legacy single-source track into a full-length clip', () => {
    const clips = clipsForTrack(track, new Map([[source.id, source]]))
    assert.equal(clips.length, 1)
    assert.deepEqual(
      { assetId: clips[0].assetId, start: clips[0].start, duration: clips[0].duration },
      { assetId: source.id, start: 0, duration: 4 },
    )
  })

  it('maps timeline time to media offset and respects clip boundaries', () => {
    const clip: EditorClip = {
      id: 'clip-a',
      assetId: source.id,
      start: 3,
      offset: 1.25,
      duration: 2,
      volume: 1,
      fadeIn: 0,
      fadeOut: 0,
      muted: false,
      locked: false,
    }
    assert.equal(clipIsActive(clip, 2.99), false)
    assert.equal(clipIsActive(clip, 3), true)
    assert.equal(clipIsActive(clip, 5), false)
    assert.ok(Math.abs(clipMediaTime(clip, 3.75) - 2) < 0.000_001)
  })

  it('uses the latest clip end as the project duration', () => {
    const secondSource = { ...source, id: 'source-b', duration: 2 }
    const mixedTrack: EditorTrack = {
      ...track,
      clips: [
        { id: 'a', assetId: source.id, start: 0, offset: 0, duration: 4, volume: 1, fadeIn: 0, fadeOut: 0, muted: false, locked: false },
        { id: 'b', assetId: secondSource.id, start: 7, offset: 0, duration: 2, volume: 1, fadeIn: 0, fadeOut: 0, muted: false, locked: false },
      ],
    }
    assert.equal(timelineDuration([mixedTrack], new Map([[source.id, source], [secondSource.id, secondSource]])), 9)
  })

  it('keeps single-clip fades canonical while preserving the legacy track fields', () => {
    const singleClipTrack: EditorTrack = {
      ...track,
      fadeIn: 0,
      fadeOut: 0,
      clips: [{
        id: 'clip-a',
        assetId: source.id,
        start: 0,
        offset: 0,
        duration: 1,
        volume: 1,
        fadeIn: 0.35,
        fadeOut: 2,
        muted: false,
        locked: false,
      }],
    }
    syncSingleClipFadeCompatibility(singleClipTrack)
    assert.equal(singleClipTrack.clips![0].fadeIn, 0.35)
    assert.equal(singleClipTrack.clips![0].fadeOut, 1)
    assert.equal(singleClipTrack.fadeIn, 0.35)
    assert.equal(singleClipTrack.fadeOut, 1)

    const multiClipTrack = {
      ...singleClipTrack,
      fadeIn: 0.1,
      clips: [singleClipTrack.clips![0], { ...singleClipTrack.clips![0], id: 'clip-b' }],
    }
    syncSingleClipFadeCompatibility(multiClipTrack)
    assert.equal(multiClipTrack.fadeIn, 0.1)
  })

  it('moves and trims clips without crossing the timeline or source boundaries', () => {
    const timing = { start: 3, offset: 1, duration: 4 }
    assert.deepEqual(resolveClipTimingEdit(timing, 'move', -5), { start: 0 })
    assert.deepEqual(resolveClipTimingEdit(timing, 'trim-start', -3), {
      start: 2,
      offset: 0,
      duration: 5,
    })
    assert.deepEqual(resolveClipTimingEdit(timing, 'trim-end', 9, 6), { duration: 5 })
    assert.deepEqual(resolveClipTimingEdit(timing, 'trim-end', -9, 6), { duration: 0.05 })
  })

  it('stops either trim edge at the nearest same-track neighbor and rejects snap targets beyond it', () => {
    const clip = { id: 'selected', start: 5, offset: 3, duration: 2 }
    const bounds = clipTrimBounds(clip, [
      { id: 'far-right', start: 12, duration: 1 },
      { id: 'left', start: 3, duration: 1 },
      clip,
      { id: 'right', start: 8, duration: 2 },
    ])
    assert.deepEqual(bounds, { start: 4, end: 8 })
    assert.deepEqual(resolveClipTimingEdit(clip, 'trim-start', -3, 20, bounds), {
      start: 4, offset: 2, duration: 3,
    })
    assert.deepEqual(resolveClipTimingEdit(clip, 'trim-end', 3, 20, bounds), { duration: 3 })
    assert.deepEqual(resolveSnappedClipTimingEdit(clip, 'trim-end', 1.04, 20, [8.04, 8], 0.08, bounds), {
      patch: { duration: 3 }, snapTime: 8,
    })
  })

  it('allows shrinking a clip with existing overlaps without extending either overlap', () => {
    const clip = { id: 'selected', start: 5, offset: 3, duration: 2 }
    const bounds = clipTrimBounds(clip, [
      { id: 'left', start: 3, duration: 3 },
      { id: 'right', start: 6, duration: 3 },
    ])
    assert.deepEqual(bounds, { start: 5, end: 7 })
    assert.deepEqual(resolveClipTimingEdit(clip, 'trim-end', 3, 20, bounds), { duration: 2 })
    assert.deepEqual(resolveClipTimingEdit(clip, 'trim-end', -1, 20, bounds), { duration: 1 })
    assert.deepEqual(resolveClipTimingEdit(clip, 'trim-start', 1, 20, bounds), {
      start: 6, offset: 4, duration: 1,
    })
  })

  it('navigates unique clip boundaries and skips the current cut', () => {
    const sources = new Map([[source.id, source]])
    const firstClip = clipsForTrack(track, sources)[0]
    const secondTrack = { ...track, id: 'second', clips: [{ ...firstClip, id: 'second', start: 4, duration: 3 }] }
    const edges = timelineClipEdges([track, secondTrack], sources)
    assert.deepEqual(edges, [0, 4, 7])
    assert.equal(adjacentClipEdge(edges, 4, -1), 0)
    assert.equal(adjacentClipEdge(edges, 4, 1), 7)
    assert.equal(adjacentClipEdge(edges, 5, -1), 4)
    assert.equal(adjacentClipEdge(edges, 7, 1), null)
    assert.deepEqual(timelineClipEdges([track, secondTrack], sources, 'second'), [0, 4])
  })

  it('marks only regions shared by multiple clips and merges nested or contiguous overlaps', () => {
    assert.deepEqual(overlappingClipRanges([
      { start: 10, duration: 2 },
      { start: 3, duration: 3 },
      { start: 0, duration: 5 },
      { start: 2, duration: 2 },
      { start: 4.5, duration: 0.25 },
      { start: 11, duration: 2 },
    ]), [{ start: 2, end: 5 }, { start: 11, end: 12 }])

    assert.deepEqual(overlappingClipRanges([
      { start: 0.1, duration: 0.2 },
      { start: 0.3, duration: 0.2 },
      { start: 0.5, duration: 0.5 },
    ]), [])
  })

  it('snaps razor cuts to the nearest reachable interior point', () => {
    const clip = { start: 3, duration: 2 }
    const targets = [3, 4, 4.25, 5]
    assert.equal(resolveSnappedSplitTime(clip, 4.2, 1 / 48_000, targets, 0.3), 4.25)
    assert.equal(resolveSnappedSplitTime(clip, 3.05, 1 / 48_000, targets, 0.3), 3.05)
    assert.equal(resolveSnappedSplitTime(clip, 4.98, 1 / 48_000, targets, 0.3), 4.98)
  })

  it('snaps either moving edge without rounding target times', () => {
    const timing = { start: 3, offset: 1, duration: 2 }
    const target = 7 + 1 / 48000
    const moved = resolveSnappedClipTimingEdit(timing, 'move', 1.97, 8, [target], 0.08)
    assert.equal(moved.snapTime, target)
    assert.ok(Math.abs(moved.patch.start! + timing.duration - target) < 1e-10)
    const start = resolveSnappedClipTimingEdit(timing, 'move', 0.97, 8, [4], 0.08)
    assert.deepEqual(start, { patch: { start: 4 }, snapTime: 4 })
  })

  it('snaps trim edges within the source and skips unreachable targets', () => {
    const timing = { start: 3, offset: 1, duration: 2 }
    assert.deepEqual(resolveSnappedClipTimingEdit(timing, 'trim-start', -0.97, 4, [1.99, 2], 0.08), {
      patch: { start: 2, offset: 0, duration: 3 }, snapTime: 2,
    })
    assert.deepEqual(resolveSnappedClipTimingEdit(timing, 'trim-end', 0.99, 4, [6.01], 0.08), {
      patch: { duration: 2.99 }, snapTime: null,
    })
    const short = { start: 1, offset: 0, duration: 1 / 48000 }
    assert.equal(resolveClipTimingEdit(short, 'trim-end', -1, 4).duration, short.duration)
  })

  it('keeps both sides of a clip when punch-in recording overwrites its middle', () => {
    const clips: EditorClip[] = [{
      id: 'original',
      assetId: source.id,
      start: 0,
      offset: 0,
      duration: 10,
      volume: 1,
      fadeIn: 0.4,
      fadeOut: 0.6,
      muted: false,
      locked: false,
    }]
    const result = overwriteClipsInRange(clips, 3, 2, () => 'right')
    assert.deepEqual(result, [
      { ...clips[0], duration: 3, fadeOut: 0 },
      { ...clips[0], id: 'right', start: 5, offset: 5, duration: 5, fadeIn: 0 },
    ])
  })

  it('removes fully covered clips and trims every partially covered edge', () => {
    const makeClip = (id: string, start: number, duration: number): EditorClip => ({
      id,
      assetId: source.id,
      start,
      offset: 0,
      duration,
      volume: 1,
      fadeIn: 0,
      fadeOut: 0,
      muted: false,
      locked: false,
    })
    const result = overwriteClipsInRange([
      makeClip('left', 0, 4),
      makeClip('covered', 4, 2),
      makeClip('right', 6, 4),
    ], 2, 6, () => 'split')
    assert.deepEqual(result.map(({ id, start, offset, duration }) => ({ id, start, offset, duration })), [
      { id: 'left', start: 0, offset: 0, duration: 2 },
      { id: 'right', start: 8, offset: 2, duration: 2 },
    ])
  })

  it('keeps punch-in recording slices within their source at sample precision', () => {
    const sampleRate = 48_000
    const duration = 16_384 / sampleRate
    const original: EditorClip = {
      id: 'recording', assetId: source.id, start: 0, offset: 0, duration,
      volume: 1, fadeIn: 0.1, fadeOut: 0.2, muted: false, locked: false,
    }
    const start = 0.05
    const end = start + 8192 / sampleRate
    const [left, right] = overwriteClipsInRange([original], start, end - start, () => 'right')
    assert.equal(left.duration, start)
    assert.equal(right.start, end)
    assert.equal(right.offset, end)
    assert.ok(right.offset + right.duration <= duration + Number.EPSILON)
    assert.ok(Math.abs(right.start + right.duration - duration) < 1 / sampleRate)
    assert.ok(left.fadeIn <= left.duration)
    assert.ok(right.fadeOut <= right.duration)
  })
})

describe('fitting clips to a shortened source', () => {
  const clip = (id: string, assetId: string, offset: number, duration: number) => ({
    id, assetId, start: 0, offset, duration, volume: 1, fadeIn: 2, fadeOut: 3, muted: false, locked: false,
  })
  const track = (clips: ReturnType<typeof clip>[]) => ({
    id: 't', sourceId: 'song', role: 'reference', name: 'Song', color: '', volume: 1, pan: 0,
    muted: false, solo: false, fadeIn: 0, fadeOut: 0, clips,
  }) as unknown as Parameters<typeof fitClipsToSource>[0][number]

  it('trims and removes clips beyond the new end and keeps fades inside the clip', () => {
    const tracks = [track([clip('whole', 'song', 0, 180), clip('tail', 'song', 150, 30), clip('other', 'other', 0, 300)])]
    assert.equal(fitClipsToSource(tracks, { id: 'song', duration: 120 }), true)
    assert.deepEqual(tracks[0]!.clips.map(item => [item.id, item.offset, item.duration]), [['whole', 0, 120], ['other', 0, 300]])
  })

  it('leaves clips that still fit untouched', () => {
    const tracks = [track([clip('intro', 'song', 10, 20)])]
    const before = tracks[0]!.clips[0]
    assert.equal(fitClipsToSource(tracks, { id: 'song', duration: 120 }), false)
    assert.equal(tracks[0]!.clips[0], before)
  })

  it('shrinks fades with the clip and mirrors them on single-clip tracks', () => {
    const tracks = [track([clip('short', 'song', 0, 60)])]
    fitClipsToSource(tracks, { id: 'song', duration: 1.5 })
    const [only] = tracks[0]!.clips
    assert.deepEqual([only!.duration, only!.fadeIn, only!.fadeOut], [1.5, 1.5, 1.5])
    assert.deepEqual([tracks[0]!.fadeIn, tracks[0]!.fadeOut], [1.5, 1.5])
  })
})
