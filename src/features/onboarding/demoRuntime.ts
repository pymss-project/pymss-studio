import { createDemoAudio, createDemoModels, createDemoWorkflow } from './demoData.ts'
import type { TutorialAdapter } from './demoContext'
import { normalizeWorkflowMetadata } from '../../workflows/metadata.ts'

export type DemoConfig = { locale: string; themeMode: string; themeAccent: string; platform: string; progress: Record<string, unknown> }
const copy = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T

export function createDemoRuntime(config: DemoConfig) {
  const parameter = config.progress.parameter
  const outputFormat = parameter === 'flacBitDepth' ? 'flac'
    : parameter === 'mp3BitRate' ? 'mp3'
    : parameter === 'm4aBitRate' || parameter === 'm4aCodec' ? 'm4a' : 'wav'
  const prefix = config.platform === 'win32' ? 'C:/Pymss-Demo' : '/Pymss-Demo'
  const input = prefix + '/sample.wav'
  const output = prefix + '/Output'
  const models = createDemoModels().map((model, index) => ({
    ...model, downloaded: true, modelPath: prefix + '/Models/' + index + '.ckpt', configPath: prefix + '/Models/' + index + '.yaml',
  }))
  const waveform = createDemoAudio()
  const outputs = ['Vocals', 'Instrumental'].map(stem => ({ stem, path: output + '/' + stem + '.wav' }))
  const now = Date.now()
  const task = { id: 'sample-separation', jobId: 'sample-job', model: models[0]!.name, input, output, status: 'done',
    progress: 100, createdAt: now, updatedAt: now, startedAt: now - 1200, finishedAt: now, durationMs: 1200,
    message: 'Done', stageLabel: 'Done', logs: [], outputs, files: outputs.map(item => item.path),
    runConfig: { runMode: 'model', modelDir: prefix + '/Models', outputLayout: 'flat', device: 'auto',
      outputFormat: 'wav', selectedStems: ['Vocals', 'Instrumental'], audioParams: {}, inferenceParams: {} } }
  const workflow = { id: 'sample-workflow', name: config.locale === 'en' ? 'Vocal separation' : '人声与伴奏分离',
    description: '', definition: createDemoWorkflow(models[0]!.name), format: 'simple', formatVersion: 1, createdAt: now, updatedAt: now }
  const initialProject = { id: 'edit_sample-separation', name: config.locale === 'en' ? 'Sample audio' : '示例音频',
    sourceTaskId: task.id, sourceResultDir: output, masterVolume: 0.8, masterPan: 0,
    sources: waveform.sources.map((source, index) => ({ ...source, path: outputs[index]!.path })),
    tracks: waveform.tracks, createdAt: now, updatedAt: now }
  const stores = new Map<string, any>([
    ['app-settings', { startupOnboardingSeen: true, locale: config.locale, themeMode: config.themeMode, themeAccent: config.themeAccent,
      animationsEnabled: false, outputDir: output, modelDir: prefix + '/Models', defaultFormat: outputFormat,
      beginnerGuideProgress: { ...config.progress, status: 'active' } }],
    ['model-state', { models, categories: ['MSS'], categoriesCn: ['MSS'], modelDir: prefix + '/Models' }],
    ['task-history', { tasks: [task] }],
    ['workflow-state', { workflows: [workflow], selectedWorkflowId: workflow.id }],
  ])
  const projects = new Map<string, any>([[initialProject.id, initialProject]])
  const callbacks = new Map<number, { handler?: (value: any) => void; once: boolean }>()
  const listeners = new Map<number, { event: string; handler: number }>()
  const timers = new Set<ReturnType<typeof setTimeout>>()
  const unknown: string[] = []
  const commands: string[] = []
  let sequence = 1
  let openPopup: (path: string) => void = () => {}
  let closePopup: () => void = () => {}
  const isMac = config.platform === 'darwin'
  const backend = isMac ? 'mlx' : 'cpu'
  const runtimeInfo = { platform: config.platform, ready: true, installedBackend: backend, torchBackend: backend,
    acceleratorAvailable: isMac, pythonVersion: '3.12', pymssVersion: '2.1.8', pymssCoreVersion: '2.1.8', pymssGraphAvailable: true,
    packages: { pymss: true, torch: !isMac, mlx: isMac }, packageVersions: { pymss: '2.1.8' },
    installedEnvironments: [{ backend, source: 'managed', health: 'ready', pythonPath: prefix + '/python', torchBackend: backend,
      packages: { pymss: true, torch: !isMac, mlx: isMac }, packageVersions: { pymss: '2.1.8' }, pymssVersion: '2.1.8', coreUpdateSupported: false }] }
  const envInfo = { platform: config.platform, pythonVersion: '3.12', pymssAvailable: true, pymssVersion: '2.1.8',
    torchAvailable: !isMac, torchVersion: '2.5', torchBackend: backend, mlxAvailable: isMac, cudaAvailable: false,
    cudaDeviceCount: 0, cudaDevices: [], mpsAvailable: false, ffmpegAvailable: true }
  function callback(handler?: (value: any) => void, once = false) {
    const id = sequence++
    callbacks.set(id, { handler, once })
    return id
  }
  function emit(event: string, payload: unknown) {
    for (const [id, listener] of [...listeners]) if (listener.event === event) {
      const entry = callbacks.get(listener.handler)
      entry?.handler?.({ id, event, payload })
      if (entry?.once) callbacks.delete(listener.handler)
    }
  }
  function later(action: () => void, delay: number) {
    const timer = setTimeout(() => { timers.delete(timer); action() }, delay)
    timers.add(timer)
  }
  const worker = (type: string, taskId: string, payload: unknown) => emit('pymss://worker-event', { type, taskId, payload })
  function startTask(payload: any) {
    for (let index = 1; index <= 5; index++) later(() => worker('task_progress', payload.taskId,
      { stage: 'separating', done: index, total: 5, unit: 'seconds' }), index * 180)
    later(() => worker('task_done', payload.taskId, { outputDir: output, outputRoot: output, outputs, files: outputs.map(item => item.path) }), 1100)
    return { taskId: payload.taskId, started: true }
  }
  function projectFor(id: string) {
    if (!projects.has(id)) projects.set(id, { ...copy(initialProject), id })
    return projects.get(id)
  }
  // A small generated asset keeps the real editor's playback and waveform paths usable without file access.
  const rate = 22050, samples = rate * 12
  const bytes = new ArrayBuffer(44 + samples * 2)
  const view = new DataView(bytes)
  const ascii = (offset: number, text: string) => [...text].forEach((character, index) => view.setUint8(offset + index, character.charCodeAt(0)))
  ascii(0, 'RIFF'); view.setUint32(4, bytes.byteLength - 8, true); ascii(8, 'WAVEfmt ')
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true)
  view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true)
  ascii(36, 'data'); view.setUint32(40, samples * 2, true)
  for (let index = 0; index < samples; index++) view.setInt16(44 + index * 2, Math.sin(index / rate * Math.PI * 440) * 1800, true)
  const audioUrl = URL.createObjectURL(new Blob([bytes], { type: 'audio/wav' }))
  function assetUrl(path: string) {
    const normalized = String(path).replace(/\\/g, '/')
    if (normalized.startsWith(prefix + '/') || normalized.startsWith('blob:')) return normalized.startsWith('blob:') ? normalized : audioUrl
    throw new Error('Only sample assets are available in this tutorial')
  }
  async function invoke(command: string, raw: unknown = {}): Promise<any> {
    const args = (raw || {}) as any
    const payload = args.payload || {}
    commands.push(command)
    if (command === 'load_app_store') return copy(stores.get(args.name) || null)
    if (command === 'save_app_store') { stores.set(args.name, copy(args.data)); return }
    if (command === 'get_app_paths') return { dataRoot: prefix, modelsDir: prefix + '/Models', outputsDir: output, editorProjectsDir: prefix + '/Projects', settingsDir: prefix + '/Settings', tempDir: prefix + '/Temp', projectsDir: prefix + '/Projects', workflowsDir: prefix + '/Workflows', logsDir: prefix + '/Logs', cacheDir: prefix + '/Cache' }
    if (command === 'update_proxy_settings') return
    if (command === 'plugin:event|listen') { const id = sequence++; listeners.set(id, { event: args.event, handler: args.handler }); return id }
    if (command === 'plugin:event|unlisten') { listeners.delete(args.eventId); return }
    if (command === 'plugin:event|emit' || command === 'plugin:event|emit_to') { emit(args.event, args.payload); return }
    if (command === 'list_models') return { models: copy(models), categories: ['MSS'], categoriesCn: ['MSS'], modelDir: prefix + '/Models' }
    if (command === 'get_model_storage_summary') {
      const items = models.map(model => ({ name: model.name, downloaded: model.downloaded, sizeBytes: model.sizeBytes,
        expectedSizeBytes: model.sizeBytes, files: [{ path: model.modelPath, sizeBytes: model.sizeBytes, exists: true }] }))
      return { modelDir: prefix + '/Models', totalBytes: items.reduce((total, model) => total + model.sizeBytes, 0),
        downloadedCount: items.length, models: copy(items), toolModels: [], toolModelsBytes: 0, residualFiles: [], residualBytes: 0 }
    }
    if (command === 'get_model_info') return copy(models.find(model => model.name === payload.model) || models[0])
    if (command === 'runtime_info') return copy(runtimeInfo)
    if (command === 'env_info' || command === 'get_env_info') return copy(envInfo)
    if (command === 'start_env_check') { later(() => emit('pymss://worker-event', { type: 'env_info', requestId: args.requestId, payload: envInfo }), 30); return }
    if (command === 'get_build_info') return { version: '0.0.12', variant: 'tutorial', official: false, updateSupported: false }
    if (command === 'get_managed_update_state') return { restartRequired: false }
    if (['pick_media_files', 'pick_audio_files'].includes(command)) return [input]
    if (['pick_single_audio_file'].includes(command)) return input
    if (command === 'scan_media_paths') return { files: [input], warnings: [] }
    if (['pick_input_folder', 'pick_output_folder', 'pick_output_dir', 'pick_model_dir'].includes(command)) return output
    if (command === 'start_separation' || command === 'start_workflow_inference') return startTask(payload)
    if (command === 'cancel_task') { worker('task_cancelled', args.taskId, {}); return true }
    if (command === 'get_workflow_editors') return { revision: 0, editors: [] }
    if (command === 'mutate_workflow_store') {
      const state = copy(stores.get('workflow-state') || { workflows: [], selectedWorkflowId: '' })
      if (payload.action === 'upsert') {
        const entry = { ...payload.entry, updatedAt: Date.now() }
        const existing = state.workflows.find((item: any) => item.id === entry.id)
        if (existing?.metadata) entry.metadata = existing.metadata
        state.workflows = [entry, ...state.workflows.filter((item: any) => item.id !== entry.id)]
        state.selectedWorkflowId = entry.id
      } else if (payload.action === 'metadata') {
        const entry = state.workflows.find((item: any) => item.id === payload.workflowId)
        const metadata = normalizeWorkflowMetadata(payload.metadata)
        if (!entry || !metadata) throw new Error('Invalid workflow metadata')
        if (payload.requireMatchingGuid && entry.metadata && entry.metadata.guid !== metadata.guid) {
          throw new Error(`WORKFLOW_METADATA_GUID_CONFLICT:${entry.metadata.guid}`)
        }
        entry.metadata = { ...metadata, guid: entry.metadata?.guid ?? metadata.guid }
      } else if (payload.action === 'select') state.selectedWorkflowId = payload.workflowId
      else if (payload.action === 'delete') state.workflows = state.workflows.filter((item: any) => item.id !== payload.workflowId)
      else throw new Error('Unknown tutorial workflow operation')
      stores.set('workflow-state', state)
      return { state }
    }
    if (command === 'open_workflow_simple_editor_window' || command === 'open_workflow_node_editor_window') {
      const path = command === 'open_workflow_node_editor_window' ? '/workflow-node-editor' : '/workflow-simple-editor'
      openPopup(path + '?' + (payload.newWorkflow ? 'new=1' : 'workflowId=' + encodeURIComponent(payload.workflowId || workflow.id)))
      return { editorSnapshot: { revision: 0, editors: [] } }
    }
    if (command === 'open_editor_window') { openPopup('/editor?projectId=' + encodeURIComponent(payload.projectId)); return }
    if (command === 'editor_project_exists') return true
    if (command === 'load_editor_project') return copy(projectFor(args.projectId))
    if (command === 'create_editor_project_from_task') return copy(projectFor('edit_' + (payload.taskId || task.id)))
    if (command === 'save_editor_project') { projects.set(args.project.id, copy(args.project)); return copy(args.project) }
    if (command === 'list_editor_projects') return [...projects.values()].map(project => ({ ...copy(project), type: 'task' }))
    if (['list_orphaned_editor_projects', 'list_open_editor_projects_for_tasks'].includes(command)) return []
    if (command === 'get_audio_metadata') return { path: payload.path, name: String(payload.path).split('/').pop(), duration: 12, sampleRate: rate, channels: 1 }
    if (command === 'generate_waveform_peaks') return { path: payload.path, peaksPath: '', peaks: waveform.sources[0]!.peaks, duration: 12, sampleRate: rate, channels: 1 }
    if (command === 'editor_preview_chunk') {
      const sampleRate = Math.max(8000, Math.min(96000, Number(args.sampleRate) || 48000))
      const channels = args.channels === 1 ? 1 : 2
      const index = Math.max(0, Number(args.index) || 0)
      const frames = Math.max(0, Math.min(4, 12 - index * 4)) * sampleRate
      const pcm = new Float32Array(frames * channels)
      const frequency = String(args.path).includes('Instrumental') ? 110 : 330
      for (let frame = 0; frame < frames; frame++) for (let channel = 0; channel < channels; channel++) {
        pcm[frame * channels + channel] = Math.sin((index * 4 + frame / sampleRate) * Math.PI * frequency * 2) * 0.06
      }
      return pcm.buffer
    }
    if (command === 'start_editor_mix_export') {
      later(() => worker('editor_mix_exported', payload.taskId, { path: output + '/mix.wav', duration: 12, sampleRate: rate, channels: 2, format: payload.format }), 300)
      return { taskId: payload.taskId, started: true }
    }
    if (command === 'reveal_path' || command === 'save_text_file_dialog') return
    if (command === 'get_runtime_env_sizes' || command === 'runtime_env_sizes') return { sizes: {}, incompleteBackends: [] }
    if (command === 'runtime_core_versions') return { packages: { pymss: { currentVersion: '2.1.8', latestVersion: '2.1.8' }, 'pymss-core': { currentVersion: '2.1.8', latestVersion: '2.1.8' } } }
    if (command === 'plugin:resources|close') return
    if (command === 'plugin:window|close') { closePopup(); return }
    if (['plugin:window|is_maximized', 'plugin:window|is_minimized', 'is_current_window_maximized'].includes(command)) return false
    if (['plugin:window|is_focused', 'plugin:window|is_visible'].includes(command)) return true
    if (['plugin:window|set_title', 'plugin:window|show', 'plugin:window|set_focus', 'plugin:window|set_decorations', 'plugin:webview|set_webview_zoom', 'debug_editor_playback', 'write_frontend_log', 'log_frontend', 'frontend_log', 'record_feature_visit'].includes(command)) return
    unknown.push(command)
    throw new Error(config.locale === 'en' ? 'This action is not available in the guided tutorial' : '此操作暂不适用于新手指引。')
  }
  return {
    invoke, callback, assetUrl, emit, models, task, workflow, initialProject, runtimeInfo, envInfo, input, output, unknown, commands, stores,
    connectWindows(open: (path: string) => void, close: () => void) { openPopup = open; closePopup = close },
    dispose() { timers.forEach(clearTimeout); timers.clear(); listeners.clear(); callbacks.clear(); URL.revokeObjectURL(audioUrl) },
  } satisfies TutorialAdapter & Record<string, unknown>
}
export type DemoRuntime = ReturnType<typeof createDemoRuntime>
