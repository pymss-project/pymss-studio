<script setup lang="ts">
import { computed, nextTick, onMounted, onScopeDispose, ref, shallowRef, watch } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import { useI18n } from 'vue-i18n'
import { AddOutline, ArrowDownOutline, ArrowUpOutline, ChevronForwardOutline, CloseOutline, FolderOpenOutline, OpenOutline, OptionsOutline, RefreshOutline, SearchOutline, Star, StarOutline, TrashOutline } from '@vicons/ionicons5'
import { useEditorStore } from '@/stores/editor'
import { useEditorPlaybackStore } from '@/stores/editorPlayback'
import { usePluginManagerLayout } from '@/composables/usePluginManagerLayout'
import { usePluginManagerWindow } from '@/composables/usePluginManagerWindow'
import type { EditorPluginSlot, EditorSession } from '@/types/editor'
import { clonePluginChain, pluginDescriptorCategory, pluginDescriptorFamilies, pluginDescriptorFormat, pluginFavoriteKey, pluginDescriptorLabels, pluginDescriptorVariant, pluginSlotMatchesDescriptor } from '@/utils/editorPlugins'
import { currentPluginSession, observePluginSession, openPluginEditor, PluginHostDialogPendingError, pluginHostCapabilities, pluginHostRequest, type PluginDescriptor, type PluginHostCapabilities, type PluginScanReport, type PluginScanStatus, type PluginScanBlacklistEntry, type PluginSessionState, type UnavailablePlugin } from '@/utils/pluginHost'
import EditorPluginParameters from './EditorPluginParameters.vue'

const props = defineProps<{ session: EditorSession; selectedTrackId: string | null }>()
const emit = defineEmits<{ selectTarget: [trackId: string | null]; togglePlayback: [] }>()
const { t, locale } = useI18n()
const editor = useEditorStore()
const playback = useEditorPlaybackStore()
const capabilities = ref<PluginHostCapabilities | null>(null)
const busy = ref(false)
const scanning = ref(false)
const catalogLoading = ref(false)
const scanStatus = ref<PluginScanStatus | null>(null)
const scanBlacklist = ref<PluginScanBlacklistEntry[]>([])
const scanActionPending = ref<'skip' | 'cancel' | null>(null)
const scanStopRequested = ref(false)
const scanFeedback = ref('')
const managerOpen = ref(false)
const managerCard = ref<{ $el: HTMLElement } | null>(null)
const manageButton = ref<{ $el: HTMLElement } | null>(null)
const reloadConfirmOpen = ref(false)
const targetMenuOpen = ref(false)
const sourcesOpen = ref(false)
const sourceButton = ref<{ $el: HTMLElement } | null>(null)
const librarySearch = ref('')
const catalogGroupBy = ref<'vendor' | 'category' | 'format' | 'favorites'>('vendor')
const favorites = ref(new Set<string>())
const expandedCatalogGroups = ref(new Set<string>())
const collapsedSearchGroups = ref(new Set<string>())
const feedback = ref('')
const error = ref('')
const dismissedOperationError = ref('')
const dialogPending = ref(false)
let dialogOperationError = false
const descriptors = ref<PluginDescriptor[]>([])
const scanReport = ref<PluginScanReport | null>(null)
const unavailablePlugins = ref<UnavailablePlugin[]>([])
const relocatingSlotId = ref<string | null>(null)
const selectedPluginId = ref<string | null>(null)
const target = ref<string>('master')
const directoryDraft = ref('')
const directories = ref<string[]>([])
const opened = ref<string[]>([])
const parameterSlotId = ref<string | null>(null)
const recoveryEdit = shallowRef<{
  project: EditorSession; trackId: string | null; host: PluginSessionState; sessionId: string;
  signature: string; change: (slots: EditorPluginSlot[], recovering?: boolean) => void;
} | null>(null)
// A stored empty list is an explicit choice; seed standard paths only for new preferences.
const STORAGE_KEY = 'pymss-plugin-library-directories-v1'
const FAVORITES_KEY = 'pymss-plugin-favorites-v1'
let libraryReady = false
let catalogRequest = 0
let scanGeneration = 0
let disposed = false
let scanSequence = 0
let blacklistGeneration = 0
type ActiveScan = { id: string; generation: number; project: EditorSession; controlled: boolean; timer: ReturnType<typeof setTimeout> | null }
let activeScan: ActiveScan | null = null
const canSkipScan = computed(() => scanning.value && scanStatus.value?.phase === 'scanning' && Boolean(scanStatus.value?.canSkip && scanStatus.value.currentPath) && !scanActionPending.value && !scanStopRequested.value)
const canStopScan = computed(() => scanning.value && !scanActionPending.value && !scanStopRequested.value && !['finished', 'cancelled'].includes(scanStatus.value?.phase || ''))
const scanPhaseLabel = computed(() => t(scanStatus.value?.phase === 'scanning' ? 'editor.plugins.scanPhaseScanning'
  : scanStatus.value?.phase === 'finished' ? 'editor.plugins.scanPhaseFinished'
  : scanStatus.value?.phase === 'cancelled' ? 'editor.plugins.scanPhaseCancelled' : 'editor.plugins.scanPhaseCollecting'))
const canChooseAudioUnits = computed(() => capabilities.value?.platform === 'macos' && capabilities.value.formats.includes('au'))
const defaultDirectories = computed(() => capabilities.value?.enabled ? capabilities.value.defaultDirectories || [] : [])
const hasMissingDefaultDirectories = computed(() => defaultDirectories.value.some(path => !directories.value.includes(path)))
const targetOptions = computed(() => [
  { label: t('editor.plugins.masterOutput'), value: 'master' },
  ...props.session.tracks.map(track => ({ label: track.name, value: `track:${track.id}` })),
])
const targetLabel = computed(() => targetOptions.value.find(option => option.value === target.value)?.label || t('editor.plugins.masterOutput'))
const targetTrackId = computed(() => target.value === 'master' ? null : target.value.slice(6))
const chain = computed(() => targetTrackId.value === null
  ? props.session.pluginChain || []
  : props.session.tracks.find(track => track.id === targetTrackId.value)?.pluginChain || [])
const parameterSlot = computed(() => chain.value.find(slot => slot.id === parameterSlotId.value))
const { managerBody, managerChain, managerStyle, libraryWidth, libraryMin, libraryMax,
  parameterHeight, parameterMin, parameterMax, resizing, stacked, startResize, resizeWithKeyboard, resetSize,
} = usePluginManagerLayout(managerOpen, computed(() => Boolean(parameterSlot.value)))
const resizeCorners = ['nw', 'ne', 'sw', 'se'] as const
const resizeCornerLabels = computed(() => ({
  nw: t('editor.plugins.resizeWindow.nw'), ne: t('editor.plugins.resizeWindow.ne'),
  sw: t('editor.plugins.resizeWindow.sw'), se: t('editor.plugins.resizeWindow.se'),
}))
const { windowStyle, resizingWindow, startWindowResize, resizeWindowWithKeyboard, resetWindowSize } = usePluginManagerWindow(managerOpen, managerCard)
const descriptorKey = (plugin: PluginDescriptor) => JSON.stringify([plugin.id, plugin.path])
const relocatingSlot = computed(() => chain.value.find(slot => slot.id === relocatingSlotId.value))
const operationError = computed(() => error.value || editor.pluginStateError || playback.error)
const showOperationError = computed(() => Boolean(operationError.value && operationError.value !== dismissedOperationError.value))
watch(operationError, () => { dismissedOperationError.value = '' })
function dismissOperationError() { dismissedOperationError.value = operationError.value || '' }
const categoryLabels = computed(() => ({
  eq: t('editor.plugins.categories.eq'), dynamics: t('editor.plugins.categories.dynamics'),
  pitch: t('editor.plugins.categories.pitch'), reverb: t('editor.plugins.categories.reverb'),
  delay: t('editor.plugins.categories.delay'), modulation: t('editor.plugins.categories.modulation'),
  distortion: t('editor.plugins.categories.distortion'), dither: t('editor.plugins.categories.dither'),
  instrument: t('editor.plugins.categories.instrument'), analyzer: t('editor.plugins.categories.analyzer'),
  mastering: t('editor.plugins.categories.mastering'), spatial: t('editor.plugins.categories.spatial'),
  ambience: t('editor.plugins.categories.ambience'), restoration: t('editor.plugins.categories.restoration'),
  offline: t('editor.plugins.categories.offline'), generator: t('editor.plugins.categories.generator'),
  tools: t('editor.plugins.categories.tools'), other: t('editor.plugins.categories.other'),
}))
const catalogOptions = computed(() => {
  const plugins = descriptors.value.filter(plugin => !relocatingSlot.value || pluginSlotMatchesDescriptor(relocatingSlot.value, plugin))
  const labels = pluginDescriptorLabels(plugins, Boolean(relocatingSlot.value))
  const descriptorLabels = new Map(plugins.map((plugin, index) => [plugin, labels[index]]))
  return pluginDescriptorFamilies(plugins).map(family => {
    const variants = family.map(plugin => {
      const variant = pluginDescriptorVariant(plugin)
      const category = pluginDescriptorCategory(plugin)
      const label = descriptorLabels.get(plugin)!
      const baseLabel = `${plugin.name} · ${variant}${plugin.vendor ? ` · ${plugin.vendor}` : ''}`
      return { label, value: descriptorKey(plugin), plugin, variant, category,
        detail: label.startsWith(`${baseLabel} · `) ? label.slice(baseLabel.length + 3) : '',
        disabled: plugin.isInstrument === true || !capabilities.value?.formats.includes(pluginDescriptorFormat(plugin)) }
    })
    const preferred = variants.find(variant => !variant.disabled) || variants[0]!
    const formatLabel = variants.map(variant => variant.variant).join(' / ')
    return { ...preferred, variants, formatLabel,
      label: variants.length > 1 ? `${preferred.plugin.name} · ${formatLabel} · ${preferred.plugin.vendor}` : preferred.label }
  })
})
const pluginOptions = computed(() => {
  const query = librarySearch.value.trim().toLocaleLowerCase()
  return catalogOptions.value.filter(option => (catalogGroupBy.value !== 'favorites' || isFavorite(option))
    && (!query || option.variants.some(variant =>
      `${variant.label} ${pluginDescriptorFormat(variant.plugin)} ${variant.category} ${categoryLabels.value[variant.category]} ${variant.plugin.subCategories?.join(' ') || ''}`.toLocaleLowerCase().includes(query))))
})
const catalogGroups = computed(() => {
  const groups = new Map<string, { key: string; label: string; unknown: boolean; plugins: typeof pluginOptions.value }>()
  const collator = new Intl.Collator(locale.value, { numeric: true, sensitivity: 'base' })
  for (const option of pluginOptions.value) {
    const vendor = option.plugin.vendor?.trim() || ''
    const key = catalogGroupBy.value === 'favorites' ? 'favorites' : catalogGroupBy.value === 'vendor' ? `vendor:${vendor.toLowerCase()}`
      : catalogGroupBy.value === 'category' ? `category:${option.category}` : `format:${option.variant}`
    const label = catalogGroupBy.value === 'favorites' ? t('editor.plugins.favorites') : catalogGroupBy.value === 'vendor' ? vendor || t('editor.plugins.unknownVendor')
      : catalogGroupBy.value === 'category' ? categoryLabels.value[option.category] : option.variant
    const unknown = catalogGroupBy.value === 'vendor' ? !vendor : catalogGroupBy.value === 'category' && option.category === 'other'
    if (!groups.has(key)) groups.set(key, { key, label, unknown, plugins: [] })
    groups.get(key)!.plugins.push(option)
  }
  for (const group of groups.values()) group.plugins.sort((a, b) => collator.compare(a.plugin.name, b.plugin.name) || collator.compare(a.variant, b.variant) || collator.compare(a.plugin.path, b.plugin.path))
  return [...groups.values()].sort((a, b) => Number(a.unknown) - Number(b.unknown) || collator.compare(a.label, b.label))
})
function isFavorite(option: (typeof catalogOptions.value)[number]) {
  return option.variants.some(variant => favorites.value.has(pluginFavoriteKey(variant.plugin)))
}
function readFavorites() {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(FAVORITES_KEY) || '[]')
    return new Set(Array.isArray(stored)
      ? stored.filter((key): key is string => typeof key === 'string' && key.length <= 1024).slice(0, 4096) : [])
  } catch { return new Set<string>() }
}
function restoreFavorites() { favorites.value = readFavorites() }
function onFavoritesStorage(event: StorageEvent) {
  if (event.key === FAVORITES_KEY || event.key === null) restoreFavorites()
}
function toggleFavorite(option: (typeof catalogOptions.value)[number]) {
  const next = readFavorites()
  const remove = isFavorite(option)
  for (const variant of option.variants) {
    const key = pluginFavoriteKey(variant.plugin)
    if (remove) next.delete(key)
    else next.add(key)
  }
  try {
    localStorage.setItem(FAVORITES_KEY, JSON.stringify([...next]))
    favorites.value = next
  } catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause) }
}
function onPluginKeydown(event: KeyboardEvent) {
  if (event.key !== ' ' || event.defaultPrevented || event.isComposing || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return
  const element = event.target as HTMLElement | null
  if (element?.isContentEditable || element?.closest?.('input, textarea, select, summary, [contenteditable]:not([contenteditable="false"]), [role="textbox"], [role="combobox"], [role="spinbutton"], [role="slider"], [role="checkbox"], [role="switch"], [role="radio"], [role="tab"], [role="menuitem"], [role="option"]')) return
  const button = element?.closest?.('button, [role="button"], a[href]')
  if (button && !button.hasAttribute('data-editor-transport')) return
  event.preventDefault()
  if (!event.repeat) emit('togglePlayback')
}
function isCatalogGroupExpanded(key: string) {
  return librarySearch.value.trim() ? !collapsedSearchGroups.value.has(key) : expandedCatalogGroups.value.has(key)
}
function toggleCatalogGroup(key: string) {
  const groups = librarySearch.value.trim() ? collapsedSearchGroups.value : expandedCatalogGroups.value
  if (groups.has(key)) groups.delete(key)
  else groups.add(key)
}
watch(librarySearch, () => collapsedSearchGroups.value.clear())
const selectedDescriptor = computed(() => descriptors.value.find(plugin => descriptorKey(plugin) === selectedPluginId.value))
const selectedFormatOptions = computed(() => pluginOptions.value.find(option => isCatalogPluginSelected(option))?.variants.map(variant => ({
  label: variant.variant, value: variant.value, disabled: variant.disabled,
})) || [])
function isCatalogPluginSelected(option: (typeof catalogOptions.value)[number]) {
  return option.variants.some(variant => variant.value === selectedPluginId.value)
}
function selectCatalogPlugin(option: (typeof catalogOptions.value)[number]) {
  if (!isCatalogPluginSelected(option)) selectedPluginId.value = option.value
}
function selectPluginFormat(value: string) {
  if (!busy.value && selectedFormatOptions.value.some(option => option.value === value && !option.disabled)) selectedPluginId.value = value
}
async function activateCatalogPlugin(option: (typeof catalogOptions.value)[number]) {
  if (busy.value || option.disabled) return
  selectCatalogPlugin(option)
  if (relocatingSlot.value) await relocatePlugin()
  else await addPlugin()
}
const stopObservingSession = observePluginSession(host => {
  const pending = recoveryEdit.value
  if (pending && (!host || host !== pending.host || host.sessionId !== pending.sessionId || !host.unavailable || host.interactionRequired)) recoveryEdit.value = null
  dialogPending.value = host?.project === props.session && Boolean(host.interactionRequired)
  if (!dialogPending.value && dialogOperationError) { error.value = ''; dialogOperationError = false }
  if (host?.project === props.session) { opened.value = [...host.openEditors]; unavailablePlugins.value = [...host.unavailablePlugins] }
  else { opened.value = []; unavailablePlugins.value = [] }
})
onScopeDispose(stopObservingSession)
watch(() => props.selectedTrackId, id => { target.value = id === null ? 'master' : `track:${id}` }, { immediate: true })
watch(() => props.session, () => { recoveryEdit.value = null; managerOpen.value = false; sourcesOpen.value = false; reloadConfirmOpen.value = false; targetMenuOpen.value = false; opened.value = []; unavailablePlugins.value = []; error.value = ''; feedback.value = ''; scanFeedback.value = ''; dialogPending.value = false; dialogOperationError = false; cancelActiveScan(); scanGeneration++; selectedPluginId.value = null; relocatingSlotId.value = null; parameterSlotId.value = null; target.value = props.selectedTrackId === null ? 'master' : `track:${props.selectedTrackId}` })
watch(targetOptions, options => { if (!options.some(option => option.value === target.value)) target.value = 'master' })
watch(target, value => {
  recoveryEdit.value = null
  reloadConfirmOpen.value = false; targetMenuOpen.value = false; relocatingSlotId.value = null; selectedPluginId.value = null; parameterSlotId.value = null; feedback.value = ''
  const trackId = value === 'master' ? null : value.slice(6)
  if (trackId !== props.selectedTrackId) emit('selectTarget', trackId)
})
watch(() => chain.value.map(slot => slot.id), ids => { if (!ids.includes(parameterSlotId.value || '')) parameterSlotId.value = null })
watch(directories, () => { invalidateCatalog(); if (libraryReady) void restoreCatalog() }, { deep: true, flush: 'sync' })
watch(pluginOptions, options => { if (!options.some(option => option.variants.some(variant => variant.value === selectedPluginId.value && !variant.disabled))) selectedPluginId.value = null })
onMounted(async () => {
  window.addEventListener('resize', dismissManagerPopovers)
  window.addEventListener('storage', onFavoritesStorage)
  restoreFavorites()
  capabilities.value = await pluginHostCapabilities()
  if (disposed) return
  let stored: unknown
  try {
    stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null')
  } catch { /* Restore standard locations when the preference cannot be read. */ }
  const sources = Array.isArray(stored) ? stored : defaultDirectories.value
  directories.value = [...new Set(sources.filter((path): path is string => typeof path === 'string' && Boolean(path.trim()) && (path !== 'au:system' || canChooseAudioUnits.value)))]
  libraryReady = true
  await Promise.all([restoreCatalog(), capabilities.value?.scanControl ? refreshBlacklist() : Promise.resolve()])
})
onScopeDispose(() => { window.removeEventListener('resize', dismissManagerPopovers); window.removeEventListener('storage', onFavoritesStorage) })
onScopeDispose(() => { disposed = true; cancelActiveScan(); scanGeneration++; catalogRequest++; blacklistGeneration++ })

function invalidateCatalog() {
  cancelActiveScan()
  scanGeneration++
  catalogRequest++
  catalogLoading.value = false
  error.value = ''
  scanReport.value = null
  descriptors.value = []
  selectedPluginId.value = null
  librarySearch.value = ''
  feedback.value = ''
  scanFeedback.value = ''
}
async function restoreCatalog() {
  if (disposed || scanning.value || !capabilities.value?.catalogCache || !directories.value.length) return
  const request = ++catalogRequest
  catalogLoading.value = true
  try {
    const report = await invoke<PluginScanReport & { hasCache: boolean }>('plugin_host_cached_scan_report', { paths: [...directories.value] })
    if (disposed || request !== catalogRequest) return
    descriptors.value = report.plugins
    scanReport.value = report.hasCache ? report : null
  } catch (cause) {
    if (!disposed && request === catalogRequest) error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    if (request === catalogRequest) catalogLoading.value = false
  }
}

function directoryLabel(path: string) {
  return path === 'au:system' ? t('editor.plugins.systemAudioUnits') : path.replace(/[\\/]+$/, '').split(/[\\/]/).at(-1) || path
}
function scanItemName(path: string, name?: string | null) {
  return path === 'au:system' ? directoryLabel(path) : name || directoryLabel(path)
}

function openManager(slotId?: string) {
  if (slotId) parameterSlotId.value = slotId
  managerOpen.value = true
  if (capabilities.value?.scanControl) void refreshBlacklist()
}
function focusManager() {
  const element = managerCard.value?.$el
  if (managerOpen.value && element && !element.contains(document.activeElement)) element.focus({ preventScroll: true })
}
function dismissManagerPopovers() {
  sourcesOpen.value = false
  targetMenuOpen.value = false
  reloadConfirmOpen.value = false
}
function managerResizeControl(event: Event) {
  const control = (event.target as Element | null)?.closest<HTMLElement>('[data-plugin-resize]')?.dataset.pluginResize
  return control === 'window' || control === 'library' || control === 'parameters' ? control : undefined
}
function onManagerPointerCapture(event: PointerEvent) {
  if (event.button === 0 && managerResizeControl(event)) dismissManagerPopovers()
}
function onManagerKeyCapture(event: KeyboardEvent) {
  const control = managerResizeControl(event)
  const resizeKeys = control === 'window' ? ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']
    : control === 'library' ? ['ArrowLeft', 'ArrowRight', 'Home', 'End'] : ['ArrowUp', 'ArrowDown', 'Home', 'End']
  if (control && !event.defaultPrevented && resizeKeys.includes(event.key)) dismissManagerPopovers()
  if (event.key !== 'Escape' || event.defaultPrevented) return
  if (!targetMenuOpen.value && !reloadConfirmOpen.value && !sourcesOpen.value) return
  if (sourcesOpen.value) void nextTick(() => sourceButton.value?.$el.focus({ preventScroll: true }))
  sourcesOpen.value = false
  targetMenuOpen.value = false
  reloadConfirmOpen.value = false
  event.preventDefault()
  event.stopPropagation()
}
function onManagerKeydown(event: KeyboardEvent) {
  onPluginKeydown(event)
  if (event.key !== 'Escape' || event.defaultPrevented) return
  const element = event.target instanceof Element ? event.target : null
  if (element?.closest('[data-plugin-confirm-trigger][aria-expanded="true"]')) {
    reloadConfirmOpen.value = false
    return
  }
  closeManager()
}
function guardConfirmationKeys(event: KeyboardEvent) {
  event.stopPropagation()
  if (event.key === 'Escape') {
    event.preventDefault()
    reloadConfirmOpen.value = false
  }
}
function closeManager() {
  const restoreFocus = managerCard.value?.$el.contains(document.activeElement)
  reloadConfirmOpen.value = false
  targetMenuOpen.value = false
  sourcesOpen.value = false
  managerOpen.value = false
  if (restoreFocus) void nextTick(() => manageButton.value?.$el.focus({ preventScroll: true }))
}

async function run(action: () => void | Promise<void>) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  dismissedOperationError.value = ''
  dialogOperationError = false
  const project = props.session
  try { await action() }
  catch (cause) { if (props.session === project) { error.value = cause instanceof Error ? cause.message : String(cause); dialogOperationError = cause instanceof PluginHostDialogPendingError } }
  finally { busy.value = false }
}
function saveDirectories() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(directories.value)) }
  catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause) }
}
function addDefaultDirectories() {
  if (busy.value) return
  directories.value = [...new Set([...directories.value, ...defaultDirectories.value])]
  saveDirectories()
}
function addDirectory() {
  const path = directoryDraft.value.trim()
  if (!path) return
  if (!directories.value.includes(path)) directories.value.push(path)
  directoryDraft.value = ''
  saveDirectories()
}
function removeDirectory(index: number) {
  if (busy.value) return
  directories.value.splice(index, 1)
  saveDirectories()
}
async function browseDirectory() {
  if (busy.value) return
  const project = props.session
  let chosen = false
  await run(async () => {
    const path = await invoke<string | null>('pick_input_folder')
    if (path && !disposed) { directoryDraft.value = path; addDirectory(); chosen = true }
  })
  if (chosen && !disposed && props.session === project) { sourcesOpen.value = false; await scan() }
}
async function chooseAudioUnits() {
  if (busy.value || !canChooseAudioUnits.value) return
  if (!directories.value.includes('au:system')) directories.value.push('au:system')
  saveDirectories()
  sourcesOpen.value = false
  await scan()
}
function isCurrentScan(scan: ActiveScan) {
  return !disposed && activeScan === scan && scan.generation === scanGeneration && props.session === scan.project
}
function clearScanPoll(scan: ActiveScan) {
  if (scan.timer) clearTimeout(scan.timer)
  scan.timer = null
}
function cancelActiveScan() {
  const scan = activeScan
  if (!scan) return
  clearScanPoll(scan)
  activeScan = null
  scanning.value = false
  scanStatus.value = null
  scanActionPending.value = null
  scanStopRequested.value = false
  if (scan.controlled) void invoke('plugin_host_cancel_scan', { scanId: scan.id }).catch(() => undefined)
}
async function pollScanStatus(scan: ActiveScan) {
  if (!isCurrentScan(scan)) return
  try {
    const status = await invoke<PluginScanStatus | null>('plugin_host_scan_status', { scanId: scan.id })
    if (!isCurrentScan(scan)) return
    if (status?.scanId === scan.id) scanStatus.value = status
  } catch (cause) {
    if (isCurrentScan(scan)) {
      scanStatus.value = null
      error.value = cause instanceof Error ? cause.message : String(cause)
    }
  } finally {
    if (isCurrentScan(scan) && !['finished', 'cancelled'].includes(scanStatus.value?.phase || '')) {
      scan.timer = setTimeout(() => { scan.timer = null; void pollScanStatus(scan) }, 400)
    }
  }
}
async function refreshBlacklist(project = props.session, generation = scanGeneration) {
  if (!capabilities.value?.scanControl || disposed) return
  const refresh = ++blacklistGeneration
  try {
    const entries = await invoke<PluginScanBlacklistEntry[]>('plugin_host_scan_blacklist')
    if (!disposed && props.session === project && scanGeneration === generation && refresh === blacklistGeneration) scanBlacklist.value = entries
  } catch (cause) {
    if (!disposed && props.session === project && scanGeneration === generation && refresh === blacklistGeneration) error.value = cause instanceof Error ? cause.message : String(cause)
  }
}
async function skipCurrentScanPlugin() {
  const scan = activeScan
  const status = scanStatus.value
  if (!scan?.controlled || !isCurrentScan(scan) || !canSkipScan.value || !status?.currentPath) return
  const path = status.currentPath
  const name = scanItemName(path, status.currentName)
  scanActionPending.value = 'skip'
  try {
    const result = await invoke<{ skipped: boolean }>('plugin_host_skip_scan', { scanId: scan.id, path })
    if (!isCurrentScan(scan)) return
    scanFeedback.value = t(result.skipped ? 'editor.plugins.scanPluginSkipped' : 'editor.plugins.scanPluginAlreadyFinished', { name })
    if (result.skipped) void refreshBlacklist(scan.project, scan.generation)
  } catch (cause) {
    if (isCurrentScan(scan)) error.value = cause instanceof Error ? cause.message : String(cause)
  } finally { if (isCurrentScan(scan)) scanActionPending.value = null }
}
async function stopScan() {
  const scan = activeScan
  if (!scan?.controlled || !isCurrentScan(scan) || !canStopScan.value) return
  scanActionPending.value = 'cancel'
  scanStopRequested.value = true
  try {
    const result = await invoke<{ cancelled: boolean }>('plugin_host_cancel_scan', { scanId: scan.id })
    if (!isCurrentScan(scan)) return
    if (!result.cancelled) scanStopRequested.value = false
    scanFeedback.value = t(result.cancelled ? 'editor.plugins.scanStopping' : 'editor.plugins.scanPhaseFinished')
  } catch (cause) {
    if (isCurrentScan(scan)) {
      scanStopRequested.value = false
      error.value = cause instanceof Error ? cause.message : String(cause)
    }
  } finally { if (isCurrentScan(scan)) scanActionPending.value = null }
}
async function removeBlacklistedPlugin(path: string) {
  if (busy.value || !capabilities.value?.scanControl) return
  await run(async () => {
    const project = props.session
    const generation = scanGeneration
    await invoke('plugin_host_remove_scan_blacklist', { path })
    if (disposed || props.session !== project || scanGeneration !== generation) return
    // A list fetched before removal completed must not put the removed entry back.
    blacklistGeneration++
    scanBlacklist.value = scanBlacklist.value.filter(entry => entry.path !== path)
    scanFeedback.value = t('editor.plugins.scanBlacklistRemoved')
  })
}
async function scan(force = false) {
  if (busy.value || scanning.value || disposed || !directories.value.length) return
  catalogRequest++
  catalogLoading.value = false
  const current: ActiveScan = { id: `plugin_scan_${Date.now().toString(36)}_${++scanSequence}_${crypto.randomUUID()}`,
    generation: ++scanGeneration, project: props.session, controlled: capabilities.value?.scanControl === true, timer: null }
  activeScan = current
  scanning.value = true
  scanStatus.value = null
  scanActionPending.value = null
  scanStopRequested.value = false
  error.value = ''
  scanFeedback.value = ''
  // Start observation before invoking the long-running scan; control commands use independent IPC calls.
  if (current.controlled) void pollScanStatus(current)
  try {
    let report = await invoke<PluginScanReport>('plugin_host_scan_report', { paths: [...directories.value], force,
      ...(current.controlled ? { scanId: current.id } : {}) })
    if (!isCurrentScan(current)) return
    if (report.cancelled) {
      const replaced = new Set([...report.plugins, ...report.failures, ...(report.blacklisted || [])].map(item => item.path))
      const retained = descriptors.value.filter(plugin => !replaced.has(plugin.path)
        && !(replaced.has('au:system') && pluginDescriptorFormat(plugin) === 'au'))
      report = { ...report, plugins: [...retained, ...report.plugins] }
      if (capabilities.value?.catalogCache) {
        try {
          const cached = await invoke<PluginScanReport>('plugin_host_cached_scan_report', { paths: [...directories.value] })
          if (!isCurrentScan(current)) return
          const failures = [...new Map([...cached.failures, ...report.failures].map(failure => [failure.path, failure])).values()]
          report = { ...report, plugins: cached.plugins, failures }
        } catch { /* Keep unvisited entries when the committed cache snapshot is temporarily unavailable. */ }
      }
    }
    if (!isCurrentScan(current)) return
    scanReport.value = report
    if (report.cancelled) scanFeedback.value = ''
    descriptors.value = report.plugins
    if (!descriptors.value.some(plugin => descriptorKey(plugin) === selectedPluginId.value)) selectedPluginId.value = null
    if (current.controlled) {
      if (report.blacklisted?.length) scanBlacklist.value = [...new Map([...scanBlacklist.value, ...report.blacklisted].map(entry => [entry.path, entry])).values()]
      void refreshBlacklist(current.project, current.generation)
    }
  } catch (cause) {
    if (isCurrentScan(current)) error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    if (activeScan === current) {
      clearScanPoll(current)
      activeScan = null
      scanning.value = false
      scanActionPending.value = null
      scanStatus.value = null
    }
  }
}
async function mutate(change: (slots: typeof chain.value, recovering?: boolean) => void, recovery = false, allowUnavailableRecovery = false) {
  await run(async () => {
    const project = props.session
    const trackId = targetTrackId.value
    try { await editor.capturePluginState(project, true, recovery) }
    catch (cause) {
      if (allowUnavailableRecovery && props.session === project && !(cause instanceof PluginHostDialogPendingError)) {
        const host = currentPluginSession()
        if (host?.project === project && !host.interactionRequired) {
          if (!host.unavailable) { try { await pluginHostRequest({ op: 'status', projectId: project.id }) } catch { /* Confirm unavailability through the session observer. */ } }
          if (host.interactionRequired) throw new PluginHostDialogPendingError()
          if (props.session === project && currentPluginSession() === host && host.unavailable && !host.interactionRequired) {
            const target = trackId === null ? project : project.tracks.find(track => track.id === trackId)
            if (target) recoveryEdit.value = { project, trackId, host, sessionId: host.sessionId, signature: JSON.stringify(target.pluginChain || []), change }
          }
        }
      }
      throw cause
    }
    if (props.session !== project) return
    const track = trackId ? props.session.tracks.find(track => track.id === trackId) : null
    if (trackId && !track) return
    const currentChain = track ? track.pluginChain : props.session.pluginChain
    const slots = clonePluginChain(currentChain) || []
    change(slots)
    if (JSON.stringify(slots) === JSON.stringify(currentChain || [])) return
    editor.setPluginChain(trackId, slots)
  })
}

async function confirmRecoveryEdit() {
  const pending = recoveryEdit.value
  if (!pending || busy.value) return false
  recoveryEdit.value = null
  await run(async () => {
    if (props.session !== pending.project) throw new Error(t('editor.plugins.stateCaptureSuperseded'))
    const target = pending.trackId === null ? pending.project : pending.project.tracks.find(track => track.id === pending.trackId)
    if (!target || JSON.stringify(target.pluginChain || []) !== pending.signature) throw new Error(t('editor.plugins.stateCaptureSuperseded'))
    const slots = clonePluginChain(target.pluginChain) || []
    pending.change(slots, true)
    await editor.recoverPluginChain({ ...pending, chain: slots })
  })
}
function beginRelocation(slotId: string) {
  relocatingSlotId.value = slotId
  selectedPluginId.value = null
  librarySearch.value = ''
}
async function relocatePlugin() {
  const plugin = descriptors.value.find(value => descriptorKey(value) === selectedPluginId.value)
  const slot = relocatingSlot.value
  if (!plugin || !slot) return
  if (!pluginSlotMatchesDescriptor(slot, plugin)) { error.value = t('editor.plugins.relocationMismatch'); return }
  await mutate(slots => {
    const current = slots.find(value => value.id === slot.id)
    if (!current || !pluginSlotMatchesDescriptor(current, plugin)) throw new Error(t('editor.plugins.relocationMismatch'))
    current.path = plugin.path
    relocatingSlotId.value = null
    selectedPluginId.value = null
  }, true)
}
async function addPlugin() {
  const plugin = descriptors.value.find(value => descriptorKey(value) === selectedPluginId.value)
  if (!plugin || plugin.isInstrument === true || !capabilities.value?.formats.includes(pluginDescriptorFormat(plugin))) return
  const project = props.session
  const trackId = targetTrackId.value
  const destination = targetLabel.value
  const slotId = `plugin_${crypto.randomUUID()}`
  await mutate(slots => slots.push({ id: slotId, format: pluginDescriptorFormat(plugin), pluginId: plugin.id,
    name: plugin.name, path: plugin.path, bypassed: false,
    ...(plugin.auVersion ? { auVersion: plugin.auVersion } : {}),
    ...(plugin.isShellChild ? { isShellChild: true } : {}) }))
  const slots = trackId ? project.tracks.find(track => track.id === trackId)?.pluginChain : project.pluginChain
  if (props.session === project && slots?.some(slot => slot.id === slotId)) feedback.value = t('editor.plugins.addedToTarget', { name: plugin.name, target: destination })
}
function setPluginBypassed(slotId: string, bypassed: boolean) {
  return mutate((slots, recovering) => {
    const slot = slots.find(slot => slot.id === slotId)
    if (slot) {
      slot.bypassed = bypassed
      if (!bypassed) delete slot.skipLoad
      else if (recovering) slot.skipLoad = true
    }
  }, bypassed, bypassed)
}
function removePlugin(slotId: string) {
  return mutate(slots => {
    const index = slots.findIndex(slot => slot.id === slotId)
    if (index >= 0) slots.splice(index, 1)
  }, true, true)
}
function movePlugin(slotId: string, offset: number) {
  return mutate(slots => {
    const index = slots.findIndex(slot => slot.id === slotId)
    if (index < 0) return
    const destination = index + offset
    if (destination < 0 || destination >= slots.length) return
    const [slot] = slots.splice(index, 1)
    slots.splice(destination, 0, slot)
  })
}
function toggleParameters(slotId: string) {
  if (!chain.value.some(slot => slot.id === slotId)) return
  parameterSlotId.value = parameterSlotId.value === slotId ? null : slotId
}
async function toggleEditor(slotId: string) {
  await run(async () => {
    const project = props.session
    const open = !opened.value.includes(slotId)
    await openPluginEditor(project, slotId, open)
    if (props.session !== project) return
    opened.value = open ? [...opened.value, slotId] : opened.value.filter(id => id !== slotId)
    if (!open) {
      await editor.capturePluginState(project, true)
      if (props.session === project) editor.scheduleSave()
    }
  })
}
</script>

<template>
  <section v-if="capabilities?.enabled" class="plugin-panel" :aria-busy="busy" :aria-label="t('editor.plugins.inserts')" @keydown.stop="onPluginKeydown">
    <div class="plugin-panel__header">
      <strong>{{ t('editor.plugins.inserts') }}<span>{{ chain.length }}</span></strong>
      <n-button ref="manageButton" size="tiny" quaternary :type="operationError ? 'warning' : 'default'" :aria-label="t('editor.plugins.title')" aria-haspopup="dialog" :aria-expanded="managerOpen" :title="t('editor.plugins.manage')" @click="openManager()"><template #icon><n-icon :component="AddOutline" /></template>{{ t('editor.plugins.library') }}</n-button>
    </div>
    <ol v-if="chain.length" class="plugin-panel__inserts" :aria-label="t('editor.plugins.chainTitle')">
      <li v-for="(slot, index) in chain" :key="slot.id" class="plugin-insert" :class="{ 'plugin-insert--bypassed': slot.bypassed }">
        <div class="plugin-insert__main">
          <span class="plugin-insert__number">{{ index + 1 }}</span>
          <n-button class="plugin-insert__editor" data-editor-transport size="small" text :type="opened.includes(slot.id) ? 'primary' : 'default'" :disabled="busy || !capabilities.nativeEditor || !(capabilities.nativeEditorFormats || ['vst3']).includes(slot.format) || unavailablePlugins.some(item => item.slotId === slot.id)" :aria-label="t(opened.includes(slot.id) ? 'editor.plugins.closeNamedEditor' : 'editor.plugins.openNamedEditor', { name: slot.name || slot.pluginId })" :title="slot.name || slot.pluginId" @click="toggleEditor(slot.id)">{{ slot.name || slot.pluginId }}</n-button>
          <n-switch size="small" :value="!slot.bypassed" :disabled="busy" :aria-label="t('editor.plugins.enableNamed', { name: slot.name || slot.pluginId })" @update:value="(enabled: boolean) => setPluginBypassed(slot.id, !enabled)" />
        </div>
        <div class="plugin-insert__tools">
          <span class="plugin-manager__format">{{ pluginDescriptorVariant({ ...slot, id: slot.pluginId }) }}</span>
          <span v-if="unavailablePlugins.some(item => item.slotId === slot.id)" class="plugin-insert__missing" :title="unavailablePlugins.find(item => item.slotId === slot.id)?.message">{{ t('editor.plugins.insertUnavailable') }}</span>
          <div>
            <n-button size="tiny" quaternary :disabled="busy || index === 0" :title="t('editor.plugins.moveUp')" :aria-label="t('editor.plugins.moveUp')" @click="movePlugin(slot.id, -1)"><template #icon><n-icon :component="ArrowUpOutline" /></template></n-button>
            <n-button size="tiny" quaternary :disabled="busy || index === chain.length - 1" :title="t('editor.plugins.moveDown')" :aria-label="t('editor.plugins.moveDown')" @click="movePlugin(slot.id, 1)"><template #icon><n-icon :component="ArrowDownOutline" /></template></n-button>
            <n-button size="tiny" quaternary :disabled="busy" :title="t('editor.plugins.parameters')" :aria-label="t('editor.plugins.parameters')" @click="openManager(slot.id)"><template #icon><n-icon :component="OptionsOutline" /></template></n-button>
            <n-button size="tiny" quaternary type="error" :disabled="busy" :title="t('editor.plugins.remove')" :aria-label="t('editor.plugins.remove')" @click="removePlugin(slot.id)"><template #icon><n-icon :component="TrashOutline" /></template></n-button>
          </div>
        </div>
      </li>
    </ol>
    <button type="button" class="plugin-panel__add" @click="openManager()"><n-icon :component="AddOutline" />{{ t('editor.plugins.addInsert') }}</button>
    <button v-if="operationError" type="button" class="plugin-panel__attention" :title="operationError" @click="openManager()">{{ t(dialogPending ? 'editor.plugins.insertWaiting' : 'editor.plugins.attentionRequired') }}</button>
  </section>

  <n-modal v-if="capabilities?.enabled" v-model:show="managerOpen" draggable
    :show-mask="false" :mask-closable="false" :trap-focus="false" :block-scroll="false"
    :auto-focus="false" :close-on-esc="false" transform-origin="center" @after-enter="focusManager">
    <template #default="{ draggableClass }">
      <n-card ref="managerCard" class="plugin-manager n-modal" :class="{ 'plugin-manager--resizing-library': resizing === 'library', 'plugin-manager--resizing-parameters': resizing === 'parameters', 'plugin-manager--resizing-nwse': resizingWindow === 'nw' || resizingWindow === 'se', 'plugin-manager--resizing-nesw': resizingWindow === 'ne' || resizingWindow === 'sw' }" :style="[managerStyle, windowStyle]" :header-class="draggableClass" content-class="plugin-manager__content" footer-class="plugin-manager__error" :bordered="false" closable tabindex="-1"
        role="dialog" :aria-modal="false" :aria-label="t('editor.plugins.managerTitle')"
        @close="closeManager" @keydown.capture="onManagerKeyCapture" @keydown.stop="onManagerKeydown"
        @pointerdown.capture="onManagerPointerCapture" @pointerdown.stop @click.stop @dblclick.stop @contextmenu.stop @wheel.stop>
        <template #header>
          <div class="plugin-manager__heading">
            <strong>{{ t('editor.plugins.managerTitle') }}</strong>
            <div class="plugin-manager__target" @pointerdown.stop @mousedown.stop>
              <label>{{ t('editor.plugins.destination') }}</label>
              <AppSelect v-model:value="target" v-model:show="targetMenuOpen" size="small" :options="targetOptions" :disabled="busy" :to="managerCard?.$el" :aria-label="t('editor.plugins.target')" />
            </div>
            <span class="plugin-manager__chain-count">{{ t('editor.plugins.chainCount', { count: chain.length }) }}</span>
          </div>
        </template>
        <template #header-extra><span class="plugin-manager__badge">{{ t('editor.plugins.experimental') }}</span></template>
        <button v-for="corner in resizeCorners" :key="corner" type="button" data-plugin-resize="window" class="plugin-manager__corner" :class="`plugin-manager__corner--${corner}`"
          :aria-label="resizeCornerLabels[corner]" :title="t('editor.plugins.resizeHint')"
          @pointerdown.stop="startWindowResize(corner, $event)" @mousedown.stop @keydown="resizeWindowWithKeyboard(corner, $event)" @dblclick.stop="resetWindowSize" />
        <div ref="managerBody" class="plugin-manager__body" :aria-busy="busy">
          <section class="plugin-manager__library" :class="{ 'plugin-manager__library--scanning': scanning }" :aria-label="t('editor.plugins.library')">
            <div class="plugin-manager__section-heading">
              <div><strong>{{ t('editor.plugins.library') }}</strong><span v-if="scanReport" class="plugin-manager__library-count" :title="t('editor.plugins.scanSummary', { scanned: scanReport.scanned, cached: scanReport.cached, found: catalogOptions.length })">{{ catalogOptions.length }}</span></div>
              <div class="plugin-manager__library-actions">
                <n-popover v-model:show="sourcesOpen" trigger="click" placement="bottom-start" :show-arrow="false" :to="managerCard?.$el" raw>
                  <template #trigger><n-button ref="sourceButton" size="tiny" quaternary :aria-expanded="sourcesOpen" :aria-label="t('editor.plugins.manageSources')"><template #icon><n-icon :component="FolderOpenOutline" /></template>{{ t('editor.plugins.sources') }}<span v-if="directories.length" class="plugin-manager__source-count">{{ directories.length }}</span></n-button></template>
                  <section class="plugin-manager__source-manager" :aria-label="t('editor.plugins.manageSources')">
                    <div class="plugin-manager__source-toolbar"><strong>{{ t('editor.plugins.manageSources') }}</strong><n-button size="tiny" quaternary :aria-label="t('common.close')" @click="sourcesOpen = false"><template #icon><n-icon :component="CloseOutline" /></template></n-button></div>
                    <div class="plugin-manager__source-content">
                      <div class="plugin-manager__sources">
                        <div class="plugin-manager__source-toolbar">
                          <span v-if="directories.length">{{ t('editor.plugins.customDirectoryCount', { count: directories.length }) }}</span>
                          <strong v-else>{{ t('editor.plugins.chooseDirectoryFirst') }}</strong>
                          <n-button size="small" :type="directories.length ? 'default' : 'primary'" :disabled="busy" @click="browseDirectory"><template #icon><n-icon :component="FolderOpenOutline" /></template>{{ directories.length ? t('editor.plugins.addDirectory') : t('editor.plugins.chooseDirectory') }}</n-button>
                        </div>
                        <p v-if="!directories.length" class="plugin-panel__hint">{{ t('editor.plugins.chooseDirectoryHint') }}</p>
                        <div v-if="directories.length" class="plugin-manager__directories">
                          <div v-for="(path, index) in directories" :key="path" class="plugin-panel__directory">
                            <span :title="path"><strong>{{ directoryLabel(path) }}</strong><small v-if="path !== 'au:system'">{{ path }}</small><small v-else class="plugin-manager__au-source-hint">{{ t('editor.plugins.scanAudioUnitsHint') }}</small></span>
                            <n-button size="tiny" quaternary :disabled="busy" :aria-label="t('editor.plugins.removeDirectory')" @click="removeDirectory(index)"><template #icon><n-icon :component="CloseOutline" /></template></n-button>
                          </div>
                        </div>
                        <n-button v-if="hasMissingDefaultDirectories" size="small" text :disabled="busy" @click="addDefaultDirectories">{{ t('editor.plugins.addDefaultDirectories') }}</n-button>
                        <n-button v-if="canChooseAudioUnits && !directories.includes('au:system')" size="small" text :disabled="busy" @click="chooseAudioUnits">{{ t('editor.plugins.chooseAudioUnits') }}</n-button>
                      </div>
                      <details v-if="capabilities.scanControl" class="plugin-manager__blacklist">
                        <summary>{{ t('editor.plugins.scanBlacklistTitle', { count: scanBlacklist.length }) }}</summary>
                        <p class="plugin-panel__hint">{{ t('editor.plugins.scanBlacklistHint') }}</p>
                        <p v-if="!scanBlacklist.length" class="plugin-panel__hint">{{ t('editor.plugins.scanBlacklistEmpty') }}</p>
                        <div v-else class="plugin-manager__blacklist-list">
                          <div v-for="entry in scanBlacklist" :key="entry.path" class="plugin-manager__blacklist-entry">
                            <span :title="entry.path"><strong>{{ scanItemName(entry.path, entry.name) }}</strong><small>{{ entry.path }}</small><small>{{ t(entry.reason === 'timeout' ? 'editor.plugins.scanBlacklistTimeout' : 'editor.plugins.scanBlacklistManual') }}</small></span>
                            <n-button size="tiny" secondary :disabled="busy" :aria-label="t('editor.plugins.removeScanBlacklistNamed', { name: scanItemName(entry.path, entry.name) })" @click="removeBlacklistedPlugin(entry.path)">{{ t('editor.plugins.removeScanBlacklist') }}</n-button>
                          </div>
                        </div>
                      </details>
                      <div v-if="scanReport?.failures.length" class="plugin-panel__scan-failures" role="status">
                        <div class="plugin-manager__scan-actions"><strong>{{ t('editor.plugins.scanFailures', { count: scanReport.failures.length }) }}</strong><n-button size="tiny" :disabled="busy" @click="sourcesOpen = false; scan(true)">{{ t('editor.plugins.retryScan') }}</n-button></div>
                        <details><summary>{{ t('editor.plugins.scanFailureDetails') }}</summary><p v-for="failure in scanReport.failures" :key="failure.path" :title="failure.path"><span>{{ failure.path }}</span>{{ failure.message }}</p></details>
                      </div>
                    </div>
                  </section>
                </n-popover>
                <n-button v-if="directories.length" size="tiny" quaternary :loading="scanning" :disabled="busy || scanning" :title="t('editor.plugins.scan')" :aria-label="t('editor.plugins.scan')" @click="scan()"><template #icon><n-icon :component="RefreshOutline" /></template></n-button>
              </div>
            </div>
            <div v-if="scanning && capabilities.scanControl" class="plugin-manager__scan-progress" :aria-label="t('editor.plugins.scanProgress')">
              <div class="plugin-manager__scan-progress-heading"><strong role="status">{{ scanPhaseLabel }}</strong><span>{{ t('editor.plugins.scanCompletedCount', { completed: scanStatus?.completed || 0, total: scanStatus?.total || 0 }) }}</span></div>
              <div v-if="scanStatus?.currentPath" class="plugin-manager__scan-current" :title="scanStatus.currentPath"><strong>{{ scanItemName(scanStatus.currentPath, scanStatus.currentName) }}</strong><small>{{ t('editor.plugins.scanElapsed', { seconds: Math.floor(scanStatus.elapsedMs / 1000) }) }}</small></div>
              <div class="plugin-manager__scan-control-actions">
                <n-button size="small" secondary type="warning" :disabled="!canSkipScan" :loading="scanActionPending === 'skip'" @click="skipCurrentScanPlugin">{{ t('editor.plugins.skipScanPlugin') }}</n-button>
                <n-button size="small" secondary :disabled="!canStopScan" :loading="scanActionPending === 'cancel'" @click="stopScan">{{ t('editor.plugins.stopScan') }}</n-button>
              </div>
              <details class="plugin-manager__scan-details">
                <summary>{{ t('editor.plugins.scanDetails') }}</summary>
                <p v-if="scanStatus?.currentPath" class="plugin-panel__hint">{{ scanStatus.currentPath }}</p>
              </details>
            </div>
            <p v-if="scanFeedback" class="plugin-manager__scan-feedback" :title="scanFeedback" role="status">{{ scanFeedback }}</p>
            <p v-if="scanReport?.cancelled" class="plugin-manager__scan-feedback" role="status">{{ t('editor.plugins.scanCancelledHint') }}</p>
            <n-button v-if="scanReport?.failures.length" size="tiny" text type="warning" class="plugin-manager__scan-warning" @click="sourcesOpen = true">{{ t('editor.plugins.scanFailures', { count: scanReport.failures.length }) }}</n-button>
            <div v-if="relocatingSlot" class="plugin-manager__relocation"><p>{{ t('editor.plugins.relocationHint', { name: relocatingSlot.name || relocatingSlot.pluginId }) }}</p><n-button size="tiny" :disabled="busy" @click="relocatingSlotId = null; selectedPluginId = null">{{ t('common.cancel') }}</n-button></div>
            <n-input v-if="descriptors.length" v-model:value="librarySearch" size="small" clearable :placeholder="t('editor.plugins.searchLibrary')" :disabled="busy"><template #prefix><n-icon :component="SearchOutline" /></template></n-input>
            <div v-if="descriptors.length" class="plugin-manager__group-controls">
              <n-radio-group v-model:value="catalogGroupBy" size="small" :disabled="busy" :aria-label="t('editor.plugins.groupBy')">
                <n-radio-button value="vendor">{{ t('editor.plugins.groupVendor') }}</n-radio-button>
                <n-radio-button value="category">{{ t('editor.plugins.groupCategory') }}</n-radio-button>
                <n-radio-button value="format">{{ t('editor.plugins.groupFormat') }}</n-radio-button>
                <n-radio-button value="favorites">{{ t('editor.plugins.favorites') }}</n-radio-button>
              </n-radio-group>
            </div>
            <div class="plugin-manager__catalog" :aria-label="t('editor.plugins.selectPlugin')" :aria-busy="scanning || catalogLoading">
              <section v-for="group in catalogGroups" :key="group.key" class="plugin-manager__group" :aria-label="group.label">
              <button v-if="catalogGroupBy !== 'favorites'" type="button" class="plugin-manager__group-heading" :aria-expanded="isCatalogGroupExpanded(group.key)" :title="group.label" @click="toggleCatalogGroup(group.key)" @keydown.space.stop @keydown.enter.stop>
                <n-icon class="plugin-manager__group-chevron" :component="ChevronForwardOutline" :size="12" aria-hidden="true" /><strong>{{ group.label }}</strong><span>{{ group.plugins.length }}</span>
              </button>
              <div v-if="catalogGroupBy === 'favorites' || isCatalogGroupExpanded(group.key)" class="plugin-manager__group-plugins" :class="{ 'plugin-manager__group-plugins--favorites': catalogGroupBy === 'favorites' }">
              <div v-for="option in group.plugins" :key="option.value" class="plugin-manager__plugin-row" :class="{ 'plugin-manager__plugin-row--selected': isCatalogPluginSelected(option) }">
              <button type="button" class="plugin-manager__plugin" data-editor-transport :class="{ 'plugin-manager__plugin--selected': isCatalogPluginSelected(option) }" :disabled="busy || option.disabled" :aria-pressed="isCatalogPluginSelected(option)" :aria-label="option.label" :title="option.label" @click="selectCatalogPlugin(option)" @dblclick.stop="activateCatalogPlugin(option)">
                <span class="plugin-manager__plugin-info"><strong>{{ option.plugin.name }}</strong><small>{{ catalogGroupBy === 'vendor' ? categoryLabels[option.category] : option.plugin.vendor || t('editor.plugins.unknownVendor') }}<template v-if="option.disabled"> · {{ t('editor.plugins.unsupportedPlugin') }}</template></small><small v-if="option.detail" class="plugin-manager__plugin-identity">{{ option.detail }}</small></span>
              </button>
              <button type="button" class="plugin-manager__favorite" :class="{ 'plugin-manager__favorite--active': isFavorite(option) }"
                :aria-pressed="isFavorite(option)" :title="t(isFavorite(option) ? 'editor.plugins.removeFavorite' : 'editor.plugins.addFavorite')"
                :aria-label="t(isFavorite(option) ? 'editor.plugins.removeFavoriteNamed' : 'editor.plugins.addFavoriteNamed', { name: option.plugin.name })"
                @click.stop="toggleFavorite(option)" @dblclick.stop @keydown.space.stop @keydown.enter.stop>
                <n-icon :component="isFavorite(option) ? Star : StarOutline" :size="16" aria-hidden="true" />
              </button>
              <span class="plugin-manager__format">{{ option.formatLabel }}</span>
              </div>
              </div>
              </section>
              <div v-if="!pluginOptions.length" class="plugin-manager__empty plugin-manager__empty--library">
                <n-icon :component="SearchOutline" :size="26" />
                <strong>{{ catalogLoading ? t('editor.plugins.restoringCatalog') : scanning ? t('editor.plugins.recognizing') : !directories.length ? t('editor.plugins.noDirectorySelected') : librarySearch.trim() ? t('editor.plugins.noPluginMatches') : catalogGroupBy === 'favorites' ? t('editor.plugins.noFavorites') : scanReport ? t('editor.plugins.noPluginsFound') : t('editor.plugins.scanToDiscover') }}</strong>
                <p>{{ !directories.length ? t('editor.plugins.supportedFormats', { formats: capabilities.formats.map(format => format.toUpperCase()).join(' / ') }) : librarySearch.trim() ? t('editor.plugins.adjustSearch') : catalogGroupBy === 'favorites' ? t('editor.plugins.favoritesHint') : t('editor.plugins.checkLocations') }}</p>
                <n-button v-if="!directories.length" size="small" secondary :disabled="busy" :title="t('editor.plugins.chooseDirectoryHint')" @click="browseDirectory"><template #icon><n-icon :component="FolderOpenOutline" /></template>{{ t('editor.plugins.chooseDirectory') }}</n-button>
                <n-button v-if="!directories.length && canChooseAudioUnits" size="small" text :disabled="busy" @click="chooseAudioUnits">{{ t('editor.plugins.chooseAudioUnits') }}</n-button>
              </div>
            </div>
            <div v-if="scanReport && descriptors.length" class="plugin-manager__library-footer">
              <div class="plugin-manager__selection" :title="selectedDescriptor?.path"><strong>{{ selectedDescriptor?.name || t('editor.plugins.selectPlugin') }}</strong><span>{{ selectedDescriptor?.path || t('editor.plugins.selectionHint') }}</span></div>
              <div v-if="selectedFormatOptions.length > 1" class="plugin-manager__format-choice">
                <span>{{ t('editor.plugins.loadFormat') }}</span>
                <AppSelect size="small" :value="selectedPluginId" :options="selectedFormatOptions" :disabled="busy" :aria-label="t('editor.plugins.loadFormat')" :to="managerCard?.$el" @update:value="selectPluginFormat" />
              </div>
              <n-button v-if="relocatingSlot" type="primary" block :disabled="busy || !selectedPluginId" @click="relocatePlugin">{{ t('editor.plugins.applyLocation') }}</n-button>
              <n-button v-else class="plugin-manager__add-button" type="primary" block :aria-label="t('editor.plugins.addPlugin', { name: targetLabel })" :disabled="busy || !selectedPluginId" @click="addPlugin"><template #icon><n-icon :component="AddOutline" /></template>{{ t('editor.plugins.addSelected') }}</n-button>
            </div>
          </section>

          <div v-if="!stacked" data-plugin-resize="library" class="plugin-manager__resize-separator plugin-manager__resize-separator--library" role="separator" tabindex="0"
            aria-orientation="vertical" :aria-label="t('editor.plugins.resizeLibrary')" :aria-description="t('editor.plugins.resizeHint')" :title="t('editor.plugins.resizeHint')"
            :aria-valuenow="libraryWidth" :aria-valuemin="libraryMin" :aria-valuemax="libraryMax"
            @pointerdown.stop="startResize('library', $event)" @mousedown.stop @keydown="resizeWithKeyboard('library', $event)" @dblclick.stop="resetSize('library')" />
          <section ref="managerChain" class="plugin-manager__chain" :class="{ 'plugin-manager__chain--parameters': parameterSlot }" :aria-label="t('editor.plugins.chainTitle')">
            <div class="plugin-manager__section-heading"><strong>{{ t('editor.plugins.chainTitle') }}</strong><span class="plugin-manager__editing-hint" :title="t('editor.plugins.editingHint')">{{ t('editor.plugins.editingHint') }}</span></div>
            <p v-if="feedback" class="plugin-manager__feedback" :title="feedback" role="status">{{ feedback }}</p>
            <div v-if="!chain.length" class="plugin-manager__empty">
              <n-icon :component="OptionsOutline" :size="28" />
              <strong>{{ t('editor.plugins.emptyChain') }}</strong>
              <p>{{ t('editor.plugins.emptyChainHint') }}</p>
            </div>
            <div v-else class="plugin-manager__slots">
            <div v-for="(slot, index) in chain" :key="slot.id" class="plugin-panel__slot" :class="{ 'plugin-panel__slot--bypassed': slot.bypassed, 'plugin-panel__slot--parameters': parameterSlotId === slot.id }">
              <div class="plugin-manager__slot-heading">
                <span class="plugin-manager__slot-number">{{ index + 1 }}</span>
                <strong :title="slot.path">{{ slot.name || slot.pluginId }}</strong>
                <span class="plugin-manager__format">{{ pluginDescriptorVariant({ ...slot, id: slot.pluginId }) }}</span>
                <n-checkbox :checked="slot.bypassed" :disabled="busy" :title="t('editor.plugins.bypassHint')" @update:checked="(value: boolean) => setPluginBypassed(slot.id, value)">{{ t('editor.plugins.bypass') }}</n-checkbox>
              </div>
              <div class="plugin-manager__slot-actions">
                <n-button size="small" secondary :type="opened.includes(slot.id) ? 'primary' : 'default'" :disabled="busy || !capabilities.nativeEditor || !(capabilities.nativeEditorFormats || ['vst3']).includes(slot.format)" @click="toggleEditor(slot.id)"><template #icon><n-icon :component="OpenOutline" /></template>{{ opened.includes(slot.id) ? t('editor.plugins.closeEditor') : t('editor.plugins.openEditor') }}</n-button>
                <n-button size="small" quaternary :type="parameterSlotId === slot.id ? 'primary' : 'default'" :aria-expanded="parameterSlotId === slot.id" :disabled="busy || !capabilities.formats.includes(slot.format) || unavailablePlugins.some(item => item.slotId === slot.id)" @click="toggleParameters(slot.id)" :aria-controls="parameterSlotId === slot.id ? 'plugin-parameter-dock' : undefined">{{ t('editor.plugins.parameters') }}</n-button>
                <div class="plugin-manager__slot-tools">
                  <n-button size="small" quaternary :disabled="busy || index === 0" :title="t('editor.plugins.moveUp')" :aria-label="t('editor.plugins.moveUp')" @click="movePlugin(slot.id, -1)"><template #icon><n-icon :component="ArrowUpOutline" /></template></n-button>
                  <n-button size="small" quaternary :disabled="busy || index === chain.length - 1" :title="t('editor.plugins.moveDown')" :aria-label="t('editor.plugins.moveDown')" @click="movePlugin(slot.id, 1)"><template #icon><n-icon :component="ArrowDownOutline" /></template></n-button>
                  <n-button v-if="slot.format !== 'au'" size="small" quaternary :disabled="busy || !capabilities.formats.includes(slot.format)" :title="t('editor.plugins.relocate')" :aria-label="t('editor.plugins.relocate')" @click="beginRelocation(slot.id)"><template #icon><n-icon :component="FolderOpenOutline" /></template></n-button>
                  <n-button size="small" quaternary type="error" :disabled="busy" :title="t('editor.plugins.remove')" :aria-label="t('editor.plugins.remove')" @click="removePlugin(slot.id)"><template #icon><n-icon :component="TrashOutline" /></template></n-button>
                </div>
              </div>
              <p v-if="unavailablePlugins.some(item => item.slotId === slot.id)" class="plugin-panel__warning">{{ t('editor.plugins.unavailableBypassed') }} {{ unavailablePlugins.find(item => item.slotId === slot.id)?.message }}</p>
            </div>
            </div>
            <div v-if="parameterSlot" data-plugin-resize="parameters" class="plugin-manager__resize-separator plugin-manager__resize-separator--parameters" role="separator" tabindex="0"
              aria-orientation="horizontal" :aria-label="t('editor.plugins.resizeParameters')" :aria-description="t('editor.plugins.resizeHint')" :title="t('editor.plugins.resizeHint')"
              :aria-valuenow="parameterHeight" :aria-valuemin="parameterMin" :aria-valuemax="parameterMax"
              @pointerdown.stop="startResize('parameters', $event)" @mousedown.stop @keydown="resizeWithKeyboard('parameters', $event)" @dblclick.stop="resetSize('parameters')" />
            <section v-if="parameterSlot" id="plugin-parameter-dock" class="plugin-manager__parameter-dock" :aria-label="t('editor.plugins.parameters')">
              <div class="plugin-manager__parameter-heading"><strong>{{ t('editor.plugins.parameters') }} · {{ parameterSlot.name || parameterSlot.pluginId }}</strong><n-button size="tiny" quaternary :aria-label="t('editor.plugins.closeParameters')" @click="parameterSlotId = null"><template #icon><n-icon :component="CloseOutline" /></template></n-button></div>
              <EditorPluginParameters :key="parameterSlot.id" :session="session" :slot-id="parameterSlot.id" :disabled="busy || unavailablePlugins.some(item => item.slotId === parameterSlot?.id)" />
            </section>
          </section>
        </div>
        <template v-if="showOperationError" #footer>
          <n-alert :type="dialogPending ? 'warning' : 'error'" :show-icon="false" :closable="!dialogPending" @close="dismissOperationError">
            {{ operationError }}
            <p v-if="!dialogPending" class="plugin-panel__hint">{{ t('editor.plugins.recoveryHint') }}</p>
            <n-button v-if="editor.pluginStateError && !dialogPending" size="small" :disabled="busy" @click="run(async () => { await editor.retryPluginStateSave() })">{{ t('editor.plugins.retrySave') }}</n-button>
            <n-popconfirm v-if="editor.pluginStateUnavailable" v-model:show="reloadConfirmOpen" :to="managerCard?.$el"
              :positive-text="t('common.confirm')" :negative-text="t('common.cancel')"
              :positive-button-props="{ onKeydown: guardConfirmationKeys }" :negative-button-props="{ onKeydown: guardConfirmationKeys }"
              @positive-click="run(async () => { await editor.restoreSavedPluginSession() })">
              <template #trigger><n-button size="small" data-plugin-confirm-trigger :aria-expanded="reloadConfirmOpen" :disabled="busy">{{ t('editor.plugins.reloadPlugins') }}</n-button></template>
              {{ t('editor.plugins.reloadWarning') }}
            </n-popconfirm>
          </n-alert>
        </template>
      </n-card>
    </template>
  </n-modal>
  <n-modal v-if="recoveryEdit" :show="true" preset="dialog" type="warning"
    :aria-label="t('editor.plugins.recoveryEditTitle')"
    :title="t('editor.plugins.recoveryEditTitle')" :content="t('editor.plugins.recoveryEditWarning')"
    :positive-text="t('editor.plugins.recoveryEditConfirm')" :negative-text="t('common.cancel')"
    :mask-closable="false" @positive-click="confirmRecoveryEdit" @negative-click="recoveryEdit = null"
    @close="recoveryEdit = null" @update:show="(value: boolean) => { if (!value) recoveryEdit = null }" />
</template>

<style scoped>
.plugin-panel { container: plugin-inserts / inline-size; min-width: 0; padding: 8px 7px; }
.plugin-panel__header { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 6px; margin-bottom: 6px; }
.plugin-panel__header > .n-button { margin-left: auto; }
.plugin-panel__header > strong { display: flex; align-items: baseline; gap: 5px; font-size: 11px; font-weight: 600; }
.plugin-panel__header > strong > span { font-size: 10px; font-weight: 400; color: var(--on-surface-muted); font-variant-numeric: tabular-nums; }
.plugin-panel__inserts { display: grid; gap: 3px; margin: 0; padding: 0; list-style: none; }
.plugin-insert { min-width: 0; padding: 3px 4px 2px; border: 1px solid var(--outline); border-left: 3px solid var(--primary); border-radius: 4px; background: var(--surface-1); }
.plugin-insert--bypassed { border-left-color: var(--outline-strong); background: transparent; }
.plugin-insert--bypassed .plugin-insert__editor { opacity: .55; }
.plugin-insert__main { display: flex; align-items: center; gap: 5px; min-width: 0; }
.plugin-insert__number { flex-shrink: 0; width: 10px; font-size: 9px; color: var(--on-surface-muted); font-variant-numeric: tabular-nums; }
.plugin-insert__editor { flex: 1; justify-content: flex-start; overflow: hidden; min-width: 0; font-size: 11px; }
.plugin-insert__editor :deep(.n-button__content) { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.plugin-insert__main > .n-switch { flex-shrink: 0; }
.plugin-insert__tools { display: flex; flex-wrap: wrap; align-items: center; gap: 3px; min-width: 0; padding-left: 15px; opacity: .5; transition: opacity 120ms; }
.plugin-insert:hover .plugin-insert__tools, .plugin-insert:focus-within .plugin-insert__tools { opacity: 1; }
.plugin-insert__tools > .plugin-manager__format { padding: 0 4px; font-size: 9px; }
.plugin-insert__tools > div { display: flex; flex-shrink: 0; gap: 0; margin-left: auto; }
.plugin-insert__tools :deep(.n-button) { width: 20px; padding: 0; }
.plugin-insert__missing { color: var(--warning); font-size: 10px; }
.plugin-panel__add { display: flex; align-items: center; justify-content: center; gap: 6px; width: 100%; min-height: 28px; margin-top: 3px; border: 1px dashed var(--outline-strong); border-radius: 4px; background: transparent; color: var(--on-surface-muted); cursor: pointer; font: inherit; font-size: 11px; }
.plugin-panel__add:hover { color: var(--primary); border-color: var(--primary-border); background: var(--primary-softer); }
.plugin-panel__attention { display: block; width: 100%; margin-top: 7px; padding: 0; border: 0; background: transparent; color: var(--warning); text-align: left; font: inherit; font-size: 11px; cursor: pointer; }
.plugin-panel__add:focus-visible, .plugin-panel__attention:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.plugin-panel__hint, .plugin-panel__warning { margin: 0; color: var(--on-surface-muted); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.plugin-panel__warning { color: var(--warning, var(--on-surface-muted)); }
.plugin-manager {
  --plugin-manager-top: 96px;
  --plugin-manager-bottom: 20px;
  --plugin-manager-inset: 24px;
  width: var(--plugin-manager-width, min(1440px, calc(100vw - var(--plugin-manager-inset) * 2)));
  height: var(--plugin-manager-height, min(860px, calc(100dvh - var(--plugin-manager-top) - var(--plugin-manager-bottom))));
  max-width: 100vw;
  max-height: 100dvh;
  margin: var(--plugin-manager-top) auto var(--plugin-manager-bottom);
  align-self: flex-start;
  min-width: 0;
  border: 1px solid var(--outline-strong);
  box-shadow: var(--shadow-soft);
}
/* Floating menus mount inside the card; its content and footer own clipping. */
.plugin-manager.n-modal { overflow: visible; }
.plugin-manager :deep(.n-card-header) { padding: 13px 16px; border-bottom: 1px solid var(--outline); border-top-left-radius: inherit; border-top-right-radius: inherit; }
.plugin-manager :deep(.plugin-manager__content) { display: flex; flex-direction: column; padding: 0; min-height: 0; overflow: hidden; border-bottom-left-radius: inherit; border-bottom-right-radius: inherit; }
.plugin-manager :deep(.plugin-manager__content:has(+ .plugin-manager__error)) { border-bottom-left-radius: 0; border-bottom-right-radius: 0; }
.plugin-manager :deep(.plugin-manager__error) { padding: 12px 20px; max-height: min(25dvh, calc(var(--plugin-manager-height, 860px) * 0.25)); overflow: auto; border-top: 1px solid var(--outline); border-bottom-left-radius: inherit; border-bottom-right-radius: inherit; }
.plugin-manager__corner { position: absolute; z-index: 2; width: 16px; height: 16px; padding: 0; border: 0; background: transparent; touch-action: none; }
.plugin-manager__corner::after { content: ''; position: absolute; inset: 4px; border-color: var(--outline-strong); border-style: solid; border-width: 0; }
.plugin-manager__corner:hover::after, .plugin-manager__corner:focus-visible::after { border-color: var(--primary); }
.plugin-manager__corner:focus-visible { outline: 2px solid var(--primary); outline-offset: -3px; }
.plugin-manager__corner--nw { top: 0; left: 0; cursor: nwse-resize; }
.plugin-manager__corner--ne { top: 0; right: 0; cursor: nesw-resize; }
.plugin-manager__corner--sw { bottom: 0; left: 0; cursor: nesw-resize; }
.plugin-manager__corner--se { bottom: 0; right: 0; cursor: nwse-resize; }
.plugin-manager__corner--nw::after { border-top-width: 1px; border-left-width: 1px; border-top-left-radius: 5px; }
.plugin-manager__corner--ne::after { border-top-width: 1px; border-right-width: 1px; border-top-right-radius: 5px; }
.plugin-manager__corner--sw::after { border-bottom-width: 1px; border-left-width: 1px; border-bottom-left-radius: 5px; }
.plugin-manager__corner--se::after { border-bottom-width: 1px; border-right-width: 1px; border-bottom-right-radius: 5px; }
.plugin-manager--resizing-nwse, .plugin-manager--resizing-nwse * { cursor: nwse-resize !important; user-select: none !important; }
.plugin-manager--resizing-nesw, .plugin-manager--resizing-nesw * { cursor: nesw-resize !important; user-select: none !important; }
.plugin-manager__heading { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px; min-width: 0; }
.plugin-manager__heading > strong { flex-shrink: 0; font-size: 16px; font-weight: 600; }
.plugin-manager__target { display: flex; align-items: center; gap: 8px; min-width: 0; }
.plugin-manager__target > label { flex-shrink: 0; font-size: 11px; color: var(--on-surface-muted); }
.plugin-manager__target > .app-select { width: clamp(140px, 18vw, 240px); max-width: 32vw; }
.plugin-manager__library-actions { display: flex; align-items: center; gap: 2px; }
.plugin-manager__source-count { margin-left: 5px; color: var(--on-surface-muted); font-variant-numeric: tabular-nums; }
/* Leave enough room for the popover to fit above or below its trigger. */
.plugin-manager__source-manager { display: flex; flex-direction: column; width: min(380px, calc(100vw - 48px)); max-height: min(420px, calc(50dvh - 32px)); color: var(--on-surface); background: var(--surface-1); border: 1px solid var(--outline-strong); border-radius: 6px; box-shadow: var(--shadow-soft); }
.plugin-manager__source-manager > .plugin-manager__source-toolbar { flex-shrink: 0; padding: 10px 12px; border-bottom: 1px solid var(--outline); }
.plugin-manager__source-content { display: grid; gap: 14px; min-height: 0; padding: 12px; overflow: auto; }
.plugin-manager__scan-details { color: var(--on-surface-muted); font-size: 11px; }
.plugin-manager__scan-details > summary { cursor: pointer; }
.plugin-manager__scan-details[open] { max-height: 110px; overflow: auto; }
.plugin-manager__scan-details p { margin-top: 5px; font-size: 11px; }
.plugin-manager__scan-feedback { flex-shrink: 0; margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--on-surface-muted); font-size: 11px; }
.plugin-manager__scan-warning { align-self: flex-start; }
.plugin-manager__editing-hint { max-width: 65%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.plugin-manager__parameter-dock { display: flex; flex-direction: column; flex: 0 0 var(--plugin-parameter-height, clamp(210px, 32dvh, 360px)); min-height: 150px; overflow: auto; }
.plugin-manager__parameter-heading { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-shrink: 0; margin-bottom: 8px; }
.plugin-manager__parameter-heading > strong { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: 12px; font-weight: 500; }
.plugin-manager__parameter-dock > .plugin-parameters { flex: 1; min-height: 0; }
.plugin-manager__parameter-dock :deep(.plugin-parameters) { max-height: none; }
.plugin-manager__chain-count { flex-shrink: 0; font-size: 11px; color: var(--on-surface-muted); font-variant-numeric: tabular-nums; }
.plugin-manager__badge { padding: 3px 7px; border-radius: 4px; background: var(--surface-2); font-size: 11px; color: var(--on-surface-muted); }
.plugin-manager__body { display: grid; grid-template-columns: var(--plugin-library-width, clamp(260px, 30%, 380px)) 8px minmax(0, 1fr); flex: 1; min-height: 0; overflow: hidden; }
.plugin-manager__resize-separator { position: relative; align-self: stretch; flex-shrink: 0; touch-action: none; user-select: none; background: transparent; transition: background 120ms; }
.plugin-manager__resize-separator::after { content: ''; position: absolute; background: var(--outline); }
.plugin-manager__resize-separator--library { width: 8px; cursor: col-resize; }
.plugin-manager__resize-separator--library::after { top: 0; bottom: 0; left: 3px; width: 1px; }
.plugin-manager__resize-separator--parameters { height: 8px; flex-basis: 8px; cursor: row-resize; }
.plugin-manager__resize-separator--parameters::after { left: 0; right: 0; top: 3px; height: 1px; }
.plugin-manager__resize-separator:hover { background: var(--primary-softer); }
.plugin-manager__resize-separator:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; background: var(--primary-softer); }
.plugin-manager__resize-separator:hover::after, .plugin-manager__resize-separator:focus-visible::after { background: var(--primary); }
.plugin-manager--resizing-library, .plugin-manager--resizing-library * { cursor: col-resize !important; user-select: none !important; }
.plugin-manager--resizing-parameters, .plugin-manager--resizing-parameters * { cursor: row-resize !important; user-select: none !important; }
.plugin-manager--resizing-library .plugin-manager__resize-separator--library,
.plugin-manager--resizing-parameters .plugin-manager__resize-separator--parameters { background: var(--primary-soft); }
.plugin-manager__chain, .plugin-manager__library { display: flex; flex-direction: column; align-items: stretch; gap: 9px; min-width: 0; min-height: 0; padding: 12px 14px; overflow: hidden; }
.plugin-manager__scan-progress { display: grid; flex-shrink: 0; gap: 6px; padding: 10px; border: 1px solid var(--outline); border-radius: 6px; background: var(--surface-2); }
.plugin-manager__scan-progress-heading { display: flex; gap: 8px; align-items: center; justify-content: space-between; font-size: 12px; }
.plugin-manager__scan-progress-heading > span { color: var(--on-surface-muted); font-variant-numeric: tabular-nums; }
.plugin-manager__scan-current { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; min-width: 0; }
.plugin-manager__scan-current > strong { min-width: 0; }
.plugin-manager__scan-current > small { flex-shrink: 0; }
.plugin-manager__blacklist-entry > span { display: grid; gap: 3px; min-width: 0; }
.plugin-manager__scan-current > *, .plugin-manager__blacklist-entry > span > * { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: 12px; }
.plugin-manager__scan-current > small, .plugin-manager__blacklist-entry small { color: var(--on-surface-muted); font-size: 11px; }
.plugin-manager__scan-control-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.plugin-manager__blacklist { font-size: 12px; }
.plugin-manager__blacklist > summary { cursor: pointer; color: var(--on-surface-muted); }
.plugin-manager__blacklist > p, .plugin-manager__blacklist-list { margin-top: 8px; }
.plugin-manager__blacklist-list { display: grid; gap: 9px; }
.plugin-manager__blacklist-entry { display: flex; gap: 8px; align-items: center; }
.plugin-manager__blacklist-entry > span { flex: 1; }
.plugin-manager__chain { background: var(--surface-1); }
.plugin-manager__chain--parameters { overflow-y: auto; }
.plugin-manager__chain--parameters > .plugin-manager__slots { min-height: 96px; }
.plugin-manager__feedback { flex-shrink: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin: 0; padding: 6px 8px; border-radius: 5px; background: var(--primary-softer); color: var(--primary-strong); font-size: 12px; line-height: 1.6; }
.plugin-manager__section-heading, .plugin-manager__slot-heading { display: flex; align-items: center; gap: 8px; min-width: 0; }
.plugin-manager__section-heading { justify-content: space-between; min-height: 28px; flex-shrink: 0; }
.plugin-manager__section-heading > div { display: flex; align-items: center; gap: 8px; }
.plugin-manager__section-heading strong { font-size: 13px; font-weight: 600; color: var(--on-surface-heading); }
.plugin-manager__section-heading > span, .plugin-manager__format, .plugin-manager__slot-number { color: var(--on-surface-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.plugin-manager__library-count { color: var(--on-surface-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.plugin-manager__empty { display: flex; flex: 1; flex-direction: column; align-items: center; justify-content: center; gap: 10px; padding: 16px; min-height: 0; text-align: center; color: var(--on-surface-muted); }
.plugin-manager__empty > .n-icon { margin-bottom: 3px; color: var(--primary); }
.plugin-manager__empty > strong { color: var(--on-surface); font-size: 13px; font-weight: 500; }
.plugin-manager__empty > p { max-width: 270px; margin: 0; font-size: 12px; line-height: 1.7; text-wrap: pretty; }
.plugin-manager__slots { display: grid; flex: 1; align-content: start; gap: 6px; min-height: 0; overflow: auto; }
.plugin-panel__slot { display: grid; gap: 5px; min-width: 0; padding: 10px; border: 1px solid var(--outline); border-radius: 5px; background: var(--surface-1); }
.plugin-panel__slot--parameters { border-color: var(--primary-border); }
.plugin-panel__slot--bypassed { border-style: dashed; }
.plugin-panel__slot--bypassed .plugin-manager__slot-heading > strong { color: var(--on-surface-muted); }
.plugin-manager__slot-number { display: grid; place-items: center; width: 23px; height: 23px; border-radius: 5px; background: var(--surface-2); flex-shrink: 0; }
.plugin-manager__slot-heading > strong { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.plugin-manager__slot-heading :deep(.n-checkbox__label) { padding-right: 0; font-size: 11px; }
.plugin-manager__format { flex-shrink: 0; padding: 2px 5px; border-radius: 4px; background: var(--surface-2); font-size: 10px; line-height: 1.5; }
.plugin-manager__slot-actions { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.plugin-manager__slot-tools { display: flex; align-items: center; gap: 2px; margin-left: auto; }
.plugin-manager__slot-tools :deep(.n-button) { width: 27px; padding: 0; }
.plugin-manager__sources { display: grid; gap: 8px; }

.plugin-manager__source-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.plugin-manager__source-toolbar > strong { font-size: 12px; font-weight: 500; }
.plugin-manager__source-toolbar > span { color: var(--on-surface-muted); font-size: 11px; }
.plugin-manager__directories { display: grid; gap: 6px; }
.plugin-panel__directory { display: flex; align-items: center; gap: 8px; min-width: 0; }
.plugin-panel__directory > span { display: grid; flex: 1; min-width: 0; line-height: 1.6; }
.plugin-panel__directory strong, .plugin-panel__directory small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.plugin-panel__directory strong { font-size: 12px; font-weight: 500; }
.plugin-panel__directory small { color: var(--on-surface-muted); font-size: 11px; }
.plugin-panel__directory .plugin-manager__au-source-hint { white-space: normal; }
.plugin-manager__scan-actions { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.plugin-panel__scan-failures { flex-shrink: 0; padding: 8px; border-radius: 6px; background: color-mix(in srgb, var(--warning) 9%, transparent); color: var(--on-surface-muted); font-size: 12px; overflow-wrap: anywhere; }
.plugin-panel__scan-failures summary { cursor: pointer; margin-top: 6px; }
.plugin-panel__scan-failures p { margin: 8px 0 0; }
.plugin-panel__scan-failures span { display: block; font-weight: 600; }
.plugin-manager__relocation { display: flex; align-items: flex-start; gap: 8px; padding: 9px; border-radius: 6px; background: var(--primary-softer); }
.plugin-manager__relocation > p { margin: 0; flex: 1; font-size: 12px; line-height: 1.6; }
.plugin-manager__catalog { flex: 1; min-height: 0; overflow: auto; }
.plugin-manager__group-controls { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-shrink: 0; }
.plugin-manager__group-controls :deep(.n-radio-group) { display: flex; width: 100%; }
.plugin-manager__group-controls :deep(.n-radio-button) { flex: 1; padding: 0 5px; text-align: center; font-size: 11px; }
.plugin-manager__group { min-width: 0; }
.plugin-manager__group + .plugin-manager__group { margin-top: 3px; }
.plugin-manager__group-heading { display: flex; align-items: center; gap: 8px; width: 100%; min-width: 0; min-height: 30px; padding: 6px 8px; border: 0; border-radius: 5px; background: transparent; color: var(--on-surface-muted); font: inherit; text-align: left; cursor: pointer; }
.plugin-manager__group-heading:hover { background: var(--surface-2); color: var(--on-surface); }
.plugin-manager__group-heading:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
.plugin-manager__group-chevron { flex-shrink: 0; transition: transform 120ms; }
.plugin-manager__group-heading[aria-expanded="true"] > .plugin-manager__group-chevron { transform: rotate(90deg); }
.plugin-manager__group-heading > strong { min-width: 0; flex: 1; font-size: 11px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.plugin-manager__group-heading > span { font-size: 10px; font-variant-numeric: tabular-nums; }
.plugin-manager__group-plugins { min-width: 0; margin: 3px 0 5px 13px; padding-left: 5px; border-left: 1px solid color-mix(in srgb, var(--outline) 50%, transparent); }
.plugin-manager__plugin-row { display: flex; align-items: center; gap: 4px; min-width: 0; margin-bottom: 3px; padding-right: 8px; border: 1px solid transparent; border-radius: 6px; transition: background 120ms, border-color 120ms; }
.plugin-manager__plugin-row:hover { background: var(--surface-2); }
.plugin-manager__plugin-row--selected, .plugin-manager__plugin-row--selected:hover { background: var(--primary-softer); border-color: var(--primary-border); }
.plugin-manager__plugin { display: flex; flex: 1; align-items: center; min-width: 0; padding: 8px; border: 0; border-radius: 5px; background: transparent; color: var(--on-surface); text-align: left; cursor: pointer; }
.plugin-manager__favorite { display: grid; place-items: center; width: 28px; height: 28px; padding: 0; flex: none; border: 0; border-radius: 4px; background: transparent; color: var(--on-surface-muted); cursor: pointer; }
.plugin-manager__favorite:hover { background: var(--surface-2); color: var(--primary); }
.plugin-manager__favorite--active { color: var(--primary); }
.plugin-manager__favorite:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
.plugin-manager__group-plugins--favorites { margin-left: 0; padding-left: 0; border-left: 0; }
.plugin-manager__plugin:disabled { cursor: default; opacity: .5; }
.plugin-manager__plugin-info { display: grid; gap: 3px; min-width: 0; flex: 1; }
.plugin-manager__plugin-info > strong { font-size: 13px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.plugin-manager__plugin-info > small { color: var(--on-surface-muted); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.plugin-manager__plugin-info > .plugin-manager__plugin-identity { white-space: normal; overflow-wrap: anywhere; line-height: 1.5; }
.plugin-manager__empty--library { min-height: 100%; padding: 12px; }
.plugin-manager__plugin:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
.plugin-manager__library-footer { display: grid; flex-shrink: 0; gap: 6px; padding-top: 8px; border-top: 1px solid var(--outline); }
.plugin-manager__library-footer > .plugin-panel__hint { font-size: 11px; }
.plugin-manager__selection { display: grid; gap: 3px; min-width: 0; }
.plugin-manager__selection > strong { font-size: 12px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.plugin-manager__selection > span { font-size: 11px; color: var(--on-surface-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.plugin-manager__format-choice { display: flex; align-items: center; gap: 8px; min-width: 0; }
.plugin-manager__format-choice > span { flex-shrink: 0; color: var(--on-surface-muted); font-size: 11px; }
.plugin-manager__format-choice :deep(.app-select) { flex: 1; min-width: 0; }
@container plugin-inserts (max-width: 145px) {
  .plugin-panel__header { gap: 4px; }
  .plugin-insert__tools { padding-left: 0; }
  .plugin-insert__main { gap: 3px; }
}
@media (max-width: 880px) {
  .plugin-manager { --plugin-manager-inset: 16px; }
  .plugin-manager__chain, .plugin-manager__library { gap: 7px; padding: 10px; }
  .plugin-manager__editing-hint { max-width: 55%; }
}
@media (max-height: 640px) {
  .plugin-manager { --plugin-manager-top: 80px; --plugin-manager-bottom: 16px; }
  .plugin-manager :deep(.n-card-header) { padding-block: 9px; }
  .plugin-manager :deep(.plugin-manager__error) { padding: 8px 12px; }
  .plugin-manager__chain, .plugin-manager__library { gap: 6px; padding: 8px 10px; }
  .plugin-manager__selection > span { font-size: 10px; }
  .plugin-panel__slot { padding: 8px; }
}
@media (max-width: 760px) {
  .plugin-manager { --plugin-manager-inset: 12px; }
  .plugin-manager__body { display: flex; flex-direction: column; overflow: auto; }
  .plugin-manager__library { flex: none; height: clamp(280px, 52dvh, 400px); }
  .plugin-manager__chain { flex: none; height: clamp(280px, 52dvh, 420px); border-left: 0; border-top: 1px solid var(--outline); }
  .plugin-manager__slots { max-height: none; }
  .plugin-manager__group-controls :deep(.n-radio-button) { white-space: normal; height: auto; min-height: 26px; line-height: 1.3; }
  .plugin-manager__library--scanning { height: auto; min-height: 240px; }
  .plugin-manager__library--scanning > .n-input,
  .plugin-manager__library--scanning > .plugin-manager__group-controls,
  .plugin-manager__library--scanning > .plugin-manager__catalog,
  .plugin-manager__library--scanning > .plugin-manager__library-footer { display: none; }
}
@media (prefers-reduced-motion: reduce) { .plugin-manager__plugin, .plugin-manager__resize-separator { transition: none; } }
</style>
