import type { Component } from 'vue'

export type AudioToolKey = 'convert' | 'merge' | 'sdr' | 'midi' | 'inspect' | 'slicer' | 'asr' | 'loudness' | 'tempo_key'
export type AudioToolCategory = 'convert' | 'analyze' | 'recognize' | 'edit'

export type AudioToolPhase =
  | 'started'
  | 'preparing'
  | 'converting'
  | 'normalizing'
  | 'merging'
  | 'loading_reference'
  | 'loading_estimated'
  | 'calculating'
  | 'loading_model'
  | 'loading_audio'
  | 'transcribing'
  | 'writing_output'
  | 'probing'
  | 'analyzing_silence'
  | 'writing_segments'
  | 'loading_asr_model'
  | 'recognizing_speech'
  | 'writing_transcript'
  | 'analyzing_tempo_key'
  | 'completed'
  | 'cancelled'
  | 'failed'

export type AudioToolFailure = { path: string; message: string }
export type AudioToolWarning = 'no_notes_detected' | 'stereo_downmix_fallback' | 'timestamps_unavailable' | string

export type AudioToolResultBase = {
  operation: AudioToolKey
  outputDir?: string
  outputPath?: string
  outputPaths?: string[]
  succeeded?: number
  failed?: AudioToolFailure[]
  skipped?: AudioToolFailure[]
  warnings?: AudioToolWarning[]
}

export type ConvertResult = AudioToolResultBase & { operation: 'convert' }
export type LoudnessResult = AudioToolResultBase & {
  operation: 'loudness'
  measurement: string
  target: number
  measurements?: Array<{
    path: string
    inputLevel: number
    outputLevel: number
    target: number
    unit: 'LUFS' | 'dB' | 'dBTP'
    withinTolerance: boolean
    outputTruePeak?: number | null
    maxTruePeak?: number
    withinTruePeakLimit?: boolean
  }>
}
export type MergeResult = AudioToolResultBase & {
  operation: 'merge'; merged?: number; sortBy?: 'name' | 'modified' | 'regex'; sortDirection?: 'asc' | 'desc'
}
export type SdrResult = AudioToolResultBase & {
  operation: 'sdr'; sampleRate: number; sdr: number[]; averageSdr: number; siSdr: number[]; averageSiSdr: number
}
export type MidiResult = AudioToolResultBase & {
  operation: 'midi'; action?: 'transcribe'; bpm: number; noteCount: number; inputDuration: number
  firstNoteAt: number | null; lastNoteAt: number | null
}
export type MidiBpmResult = AudioToolResultBase & {
  operation: 'midi'; action: 'detect_bpm'; bpm: number; roundedBpm: number
}
export type AudioFormatInfo = {
  name: string; longName: string; duration: number | null; startTime: number | null
  bitRate: number | null; streamCount: number | null; probeScore: number | null; tags: Record<string, string>
}
export type AudioStreamInfo = {
  index: number | null; codec: string; codecLongName: string; profile: string; sampleFormat: string
  sampleRate: number | null; channels: number | null; channelLayout: string; bitsPerSample: number | null
  bitRate: number | null; startTime: number | null; duration: number | null; timeBase: string
  frameCount: number | null; default: boolean; tags: Record<string, string>
}
export type InspectResult = AudioToolResultBase & {
  operation: 'inspect'; inputPath: string; fileName: string; fileSize: number
  format: AudioFormatInfo; audioStreams: AudioStreamInfo[]; chapterCount: number; raw: Record<string, unknown>
}
export type SliceSegment = { sourcePath: string; outputPath: string; start: number; end: number; duration: number }
export type SlicerResult = AudioToolResultBase & {
  operation: 'slicer'; sourceCount: number; segments: SliceSegment[]; keptDuration: number
}
export type AsrSentence = { start: number; end: number; text: string }
export type AsrResult = AudioToolResultBase & {
  operation: 'asr'; text: string; sentences: AsrSentence[]; segmentCount: number
  modelPreset?: string; requestedLanguage?: string; detectedLanguage?: string
}
export type TempoKeyItem = { path: string; fileName: string; bpm: number; key: string; duration: number }
export type TempoKeyResult = AudioToolResultBase & { operation: 'tempo_key'; items: TempoKeyItem[] }
export type AudioToolResult = TempoKeyResult | ConvertResult | LoudnessResult | MergeResult | SdrResult | MidiResult | MidiBpmResult | InspectResult | SlicerResult | AsrResult

export type AudioToolProgress = { completed: number; total: number; current: string; phase: AudioToolPhase }
export type AudioToolLogEntry = AudioToolProgress & {
  id: number; timestamp: number; updatedAt: number; description: string; detail?: string
}
export type AudioToolActivityState = { progress: AudioToolProgress; logs: AudioToolLogEntry[]; elapsedMs: number }
export type AudioToolRecovery = {
  action: 'redownload_asr_models'
  tool: 'asr'
  modelIds: string[]
  modelDir: string
  reason: 'disk_full' | 'incomplete_download'
}
export type AudioToolViewState = {
  busy: boolean; cancelling: boolean; anyBusy: boolean; hasResult: boolean; error: string; progress: AudioToolProgress
  percentage: number; result: AudioToolResult | null; elapsedMs: number; logs: AudioToolLogEntry[]
  recovery: AudioToolRecovery | null
}
export type AudioToolDefinition = {
  id: AudioToolKey; category: AudioToolCategory; titleKey: string; descriptionKey: string
  icon: Component; component: Component; hidden?: boolean
}
