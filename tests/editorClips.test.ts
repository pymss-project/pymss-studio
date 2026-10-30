import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import {
  clipIsActive,
  clipMediaTime,
  clipsForTrack,
  overwriteClipsInRange,
  resolveClipTimingEdit,
  syncSingleClipFadeCompatibility,
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
