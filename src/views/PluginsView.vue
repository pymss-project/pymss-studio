<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref, watch, type Component } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { invoke } from '@tauri-apps/api/core'
import { open } from '@tauri-apps/plugin-dialog'
import { NIcon, useDialog, useMessage, type DropdownOption, type DropdownDividerOption } from 'naive-ui'
import { ExtensionPuzzleOutline, RefreshOutline, SearchOutline, FolderOpenOutline, EllipsisHorizontalOutline, TerminalOutline, BuildOutline, TrashOutline, CheckmarkCircleOutline, PowerOutline, ChevronDownOutline, OpenOutline, ArrowForwardOutline, ListOutline, GridOutline } from '@vicons/ionicons5'
import { usePluginMarketplace, type CatalogPlugin, type InstalledPlugin, type PluginAction, type PluginCompatibility, type PluginUpdate } from '@/features/plugin-marketplace/composable'
import { useAppStore } from '@/stores/app'
import { useSettingsStore } from '@/stores/settings'
import PluginLogDialog from '@/features/plugin-marketplace/PluginLogDialog.vue'
import { normalizePluginCategory, pluginCategories, pluginLocale, pluginSearchText, pluginViewTitle } from '@/features/plugin-marketplace/presentation'
import { pluginToolCategories } from '@/features/plugin-marketplace/toolEntries'
import { canApplyPluginUpdate, canCheckPluginUpdate, pluginUpdateKey, pluginUpdateState } from '@/features/plugin-marketplace/updates'
import { buildPluginLibrary, installedPluginKey, pluginNeedsAttention } from '@/features/plugin-marketplace/library'

const { t, locale } = useI18n()
const router = useRouter()
const dialog = useDialog()
const message = useMessage()
const app = useAppStore()
const settings = useSettingsStore()
const preparationSources = ref<Record<string, string | null>>({})
const { snapshot, updates, loading, catalogLoading, error, busy, exclusiveBusy, logs, logRecords, activity, activities, openMarketplace, refreshInventory, clearUpdateChecks, manage, cancel } = usePluginMarketplace()
const statusFilter = ref<'all' | 'installed' | 'available' | 'attention'>('all')
const recentInstalls = ref(new Map<string, string>())
const pageElement = ref<HTMLElement | null>(null)
const detailKey = ref('')
const showDetails = ref(false)
const cardView = computed(() => settings.pluginViewMode === 'card')
const pluginMenu = ref<{ key: string; runtime: string; python: string; x: number; y: number; trigger: HTMLElement; mode: 'actions' | 'views' } | null>(null)
const search = ref('')
const category = ref('all')
const switchingSource = ref(false)
const sourceOptions = computed(() => [
  { label: t('plugins.sourceAuto'), value: 'auto' },
  { label: 'GitHub', value: 'github' },
  { label: 'GitCode', value: 'gitcode', disabled: snapshot.value?.catalogSource?.gitAvailable === false },
])
const customSource = computed(() => snapshot.value?.catalogSource?.provider === 'custom')
const sourceStatus = computed(() => {
  const provider = snapshot.value?.catalogSource?.provider
  if (!provider) return t('plugins.sourceUnconfirmed')
  const source = provider === 'custom' ? t('plugins.customSource') : provider === 'github' ? 'GitHub' : 'GitCode'
  return t(catalogLoading.value || snapshot.value?.catalogError ? 'plugins.sourceLastConfirmed' : 'plugins.sourceSelected', { source })
})

async function changePluginSource(value: 'auto' | 'github' | 'gitcode') {
  if (value === settings.pluginSource || busy.value || switchingSource.value) return
  switchingSource.value = true
  try {
    const affectsCatalog = !customSource.value
    await settings.savePluginSourcePreference(value)
    if (affectsCatalog) {
      clearUpdateChecks()
      recentInstalls.value = new Map()
      await openMarketplace(true)
    }
  } catch (failure) {
    message.error(String(failure))
  } finally {
    switchingSource.value = false
  }
}
const categoryLabels = { convert: 'tools.categoryConvert', analyze: 'tools.categoryAnalyze', recognize: 'tools.categoryRecognize', edit: 'tools.categoryEdit' }
const categoryOptions = computed(() => [{ label: t('plugins.allCategories'), value: 'all' },
  ...pluginCategories.map(value => ({ label: t(categoryLabels[value]), value })), { label: t('plugins.otherCategory'), value: 'other' }])
const source = ref('')
const showSource = ref(false)
const showLogs = ref(false)
const logTaskId = ref<string>()
const showUpdate = ref(false)
const updateName = ref('')
const updateRuntime = ref('')
const updateInstallation = ref('')
const updateMode = ref<'update' | 'refetch'>('update')
const reviewedUpdate = ref<PluginUpdate | null>(null)
const installDependencies = ref(false)
const updatePlugin = computed(() => snapshot.value?.installed.find(plugin => plugin.managed && plugin.name === updateName.value))
const removedPreinstalled = computed(() => (snapshot.value?.preinstalled || [])
  .filter(plugin => !plugin.installed
    && !snapshot.value?.preinstalledAvailable?.includes(plugin.name)
    && !snapshot.value?.installed.some(item => item.managed && item.name === plugin.name))
  .map(plugin => ({ ...snapshot.value?.catalog.find(item => item.name === plugin.name), ...plugin })))
const bundledOffers = computed(() => new Map(removedPreinstalled.value.map(plugin => [plugin.name, plugin])))
const libraryEntries = computed(() => {
  const offers: CatalogPlugin[] = [...(snapshot.value?.catalog || [])]
  const names = new Set(offers.map(plugin => plugin.name))
  for (const plugin of removedPreinstalled.value) if (!names.has(plugin.name)) {
    offers.push({ name: plugin.name, url: '', version: plugin.version, description: plugin.description || '',
      tags: plugin.tags || [], locales: plugin.locales, category: plugin.category })
  }
  return buildPluginLibrary(snapshot.value?.installed || [], offers)
})
const detailEntry = computed(() => libraryEntries.value.find(entry => entry.key === detailKey.value))
const detailPlugin = computed(() => detailEntry.value?.installed)
const detailOffer = computed(() => detailEntry.value?.catalog)
const matchingEntries = computed(() => libraryEntries.value.filter(entry => {
  const text = [entry.installed && pluginSearchText(entry.installed), entry.catalog && pluginSearchText(entry.catalog)].filter(Boolean).join(' ')
  return text.includes(search.value.trim().toLowerCase()) && entryCategories(entry.installed, entry.catalog).some(categoryMatches)
}))
const attentionCount = computed(() => matchingEntries.value.filter(entry => entry.installed && pluginNeedsAttention(entry.installed, updates.value[entry.name])).length)
const statusOptions = computed(() => [
  { label: t('plugins.allPlugins'), value: 'all' },
  { label: `${t('plugins.installed')} (${matchingEntries.value.filter(entry => entry.installed).length})`, value: 'installed' },
  { label: `${t('plugins.notInstalled')} (${matchingEntries.value.filter(entry => !entry.installed).length})`, value: 'available' },
  { label: t('plugins.needsAttention', { count: attentionCount.value }), value: 'attention' },
])
const visibleEntries = computed(() => {
  const collator = new Intl.Collator(locale.value, { numeric: true, sensitivity: 'base' })
  return matchingEntries.value.filter(entry => statusFilter.value === 'all'
    || (statusFilter.value === 'installed' && !!entry.installed)
    || (statusFilter.value === 'available' && !entry.installed)
    || (statusFilter.value === 'attention' && entry.installed && pluginNeedsAttention(entry.installed, updates.value[entry.name])))
    .map(entry => ({ entry, rank: entry.installed && !recentInstalls.value.has(entry.key) ? 0 : 1,
      title: recentInstalls.value.get(entry.key) ?? (entry.installed ? pluginTitle(entry.installed) : catalogTitle(entry.catalog!)) }))
    .sort((a, b) => a.rank - b.rank || collator.compare(a.title, b.title) || a.entry.key.localeCompare(b.entry.key)).map(item => item.entry)
})
const installed = computed(() => visibleEntries.value.flatMap(entry => entry.installed ? [entry.installed] : []))
const updateCount = computed(() => Object.values(updates.value).filter(canApplyPluginUpdate).length)
const updateErrorCount = computed(() => Object.values(updates.value).filter(update => pluginUpdateState(update) === 'error').length)
const lastUpdateCheck = computed(() => {
  const dates = Object.values(updates.value).map(update => Date.parse(update.checked_at || '')).filter(Number.isFinite)
  return dates.length ? new Intl.DateTimeFormat(locale.value, { dateStyle: 'short', timeStyle: 'short' }).format(Math.max(...dates)) : ''
})
const updateCheck = computed(() => activities.value.find(task => task.action === 'check_updates' && (task.status === 'running' || task.status === 'cancelling')))
const activeTasks = computed(() => activities.value.filter(task => task.status === 'running' || task.status === 'cancelling'))
const disabled = computed(() => busy.value || loading.value || catalogLoading.value || snapshot.value?.runtime.managementSupported === false)

function pluginUpdate(plugin: InstalledPlugin) { return plugin.managed ? updates.value[plugin.name] : undefined }
function checkingUpdate(plugin: InstalledPlugin) { return plugin.managed && !!updateCheck.value && (!updateCheck.value.name || updateCheck.value.name === plugin.name) }
function checkedTime(update?: PluginUpdate) {
  const time = Date.parse(update?.checked_at || '')
  return Number.isFinite(time) ? new Intl.DateTimeFormat(locale.value, { dateStyle: 'short', timeStyle: 'short' }).format(time) : ''
}
function updateStatusText(plugin: InstalledPlugin) {
  if (!canCheckPluginUpdate(plugin)) return t('plugins.updateUnsupported')
  if (checkingUpdate(plugin)) return t('plugins.updateChecking')
  const update = pluginUpdate(plugin)
  switch (pluginUpdateState(update)) {
    case 'update': return t('plugins.updateAvailable', { version: update?.remote_version })
    case 'content': return t('plugins.filesChanged')
    case 'source_changed': return t('plugins.updateSourceChanged')
    case 'older': return t('plugins.updateOlder', { version: update?.remote_version })
    case 'incomparable': return t('plugins.updateIncomparable', { version: update?.remote_version })
    case 'latest': return t('plugins.upToDate')
    case 'error': return t('plugins.updateCheckFailed')
    default: return t('plugins.updateUnchecked')
  }
}

function pluginActivity(name: string) {
  return [...activities.value].reverse().find(task => task.name === name && (!task.runtime || task.runtime === snapshot.value?.runtime.identity))
}
function activePluginTask(plugin: InstalledPlugin) {
  if (!plugin.managed) return undefined
  const task = pluginActivity(plugin.name)
  return task?.status === 'running' || task?.status === 'cancelling' ? task : undefined
}
function openPluginLogs(id = activeTasks.value.at(-1)?.id || activity.value?.id) {
  logTaskId.value = id
  showLogs.value = true
}
function installing(name: string) {
  const task = pluginActivity(name)
  return task?.status === 'running' || task?.status === 'cancelling'
}
function installDisabled(name = '') {
  return exclusiveBusy.value || loading.value || catalogLoading.value || snapshot.value?.runtime.managementSupported === false || (name ? installing(name) : false)
}
function installLabel(name: string) {
  return installing(name) ? t(pluginActivity(name)?.phase === 'queued' ? 'plugins.phases.queued' : 'plugins.installing') : t('plugins.install')
}

onMounted(openMarketplace)
watch(() => [app.runtimeInfo?.backend, app.runtimeInfo?.installState?.pythonPath], () => {
  closePluginMenu()
  if (!busy.value) void openMarketplace(true)
})
watch([statusFilter, search, category, installed, () => snapshot.value, () => settings.pluginViewMode], () => closePluginMenu(), { flush: 'sync' })
watch(detailEntry, entry => { if (!entry) showDetails.value = false })
watch(statusFilter, () => { recentInstalls.value = new Map() }, { flush: 'sync' })
watch([() => snapshot.value?.runtime.identity, () => snapshot.value?.runtime.pythonPath, () => snapshot.value?.runtime.updateSourceIdentity], () => {
  closePluginMenu()
  showDetails.value = false
  recentInstalls.value = new Map()
}, { flush: 'sync' })
onMounted(() => {
  window.addEventListener('scroll', closePluginMenuOnScroll, true)
  window.addEventListener('resize', closePluginMenuOnResize)
  window.addEventListener('keydown', closePluginMenuOnKeydown, true)
})
onBeforeUnmount(() => {
  closePluginMenu()
  window.removeEventListener('scroll', closePluginMenuOnScroll, true)
  window.removeEventListener('resize', closePluginMenuOnResize)
  window.removeEventListener('keydown', closePluginMenuOnKeydown, true)
})

async function perform(action: PluginAction, name?: string, target?: string, options: { installDependencies?: boolean; expectedRuntime?: string; expectedUpdate?: PluginUpdate } = {}) {
  if (action === 'install' ? installDisabled(name || target) : disabled.value) { message.warning(t('plugins.busyRetry')); return }
  try {
    const operation = manage(action, { name, target, locale: locale.value, ...options })
    await operation
    message.success(t('plugins.actionComplete'))
    return true
  } catch (failure) {
    // Worker failures retain their detailed error across navigation.
    if (!error.value && activity.value?.status !== 'cancelled') message.error(String(failure))
    return false
  }
}

function install(name?: string, target?: string) {
  if (name && snapshot.value?.installed.some(plugin => plugin.managed && plugin.name === name)) return
  if (name && incompatible(snapshot.value?.catalog.find(plugin => plugin.name === name))) return
  const entry = name ? libraryEntries.value.find(item => item.catalog?.name === name && !item.installed) : undefined
  const resolved = target?.trim() || name || ''
  if (!resolved) return
  const expectedRuntime = snapshot.value?.runtime.identity
  dialog.info({
    title: t('plugins.installTitle'),
    content: t('plugins.installConfirm', { source: resolved }),
    positiveText: t('plugins.install'), negativeText: t('common.cancel'),
    onPositiveClick: () => {
      showSource.value = false
      source.value = ''
      if (entry) recentInstalls.value = new Map(recentInstalls.value).set(entry.key, catalogTitle(entry.catalog!))
      void perform('install', name, resolved, { expectedRuntime }).then(success => {
        if (!success && entry) forgetRecentInstall(entry.key)
      })
    },
  })
}

function uninstall(name: string) {
  const expectedRuntime = snapshot.value?.runtime.identity
  let confirmed = false
  dialog.warning({
    title: t('plugins.uninstall'), content: t('plugins.uninstallConfirm', { name }),
    positiveText: t('plugins.uninstall'), negativeText: t('common.cancel'),
    onPositiveClick: () => {
      confirmed = true
      void perform('uninstall', name, undefined, { expectedRuntime })
    },
    onAfterLeave: () => { if (confirmed) openPluginLogs(pluginActivity(name)?.id) },
  })
}

async function chooseFolder() {
  const selected = await open({ directory: true, multiple: false, title: t('plugins.chooseFolder') })
  if (typeof selected === 'string') source.value = selected
}

function categoryMatches(value: unknown) {
  return category.value === 'all' || category.value === (normalizePluginCategory(value) || 'other')
}
function categoryLabel(value: unknown) {
  const normalized = normalizePluginCategory(value)
  return t(normalized ? categoryLabels[normalized] : 'plugins.otherCategory')
}
function catalogTitle(plugin: { name: string; locales?: InstalledPlugin['locales'] }) {
  return pluginLocale(plugin.locales, locale.value).title || plugin.name
}
function sourceLabel(url: string) {
  return /^(?:[a-zA-Z]:[\\/]|\/|\\\\|file:)/.test(url) ? t('plugins.localSource') : url.replace('https://github.com/', '')
}
function incompatible(plugin?: { compatibility?: PluginCompatibility }) {
  return plugin?.compatibility?.compatible === false
}

function openPluginView(plugin: { name: string }, view: { id: string; placements?: string[] }) {
  const placements = view.placements || ['tool']
  if (placements.includes('workspace')) {
    void router.push({ name: 'plugin-view', params: { plugin: plugin.name, viewId: view.id } })
  } else {
    void router.push({ name: 'tools', query: { pluginView: `${plugin.name}/${view.id}` } })
  }
}
function pluginTitle(plugin: { name: string; locales?: InstalledPlugin['locales']; contributions?: InstalledPlugin['contributions'] }) {
  return pluginLocale(plugin.locales, locale.value).title || plugin.contributions?.views?.find(view => view.id === 'tool')?.title || plugin.name
}
function pluginDescription(plugin: InstalledPlugin) {
  return pluginLocale(plugin.locales, locale.value).description || plugin.description || ''
}
function operationLabels(plugin: InstalledPlugin) {
  const titles = new Map(Object.entries(plugin.operationTitles || {}))
  for (const [id, title] of Object.entries(pluginLocale(plugin.locales, locale.value).operations || {})) titles.set(id, title)
  return (plugin.operations || []).map(id => ({ id, title: titles.get(id) || id }))
}
function pluginKey(plugin: Pick<InstalledPlugin, 'name' | 'managed' | 'path'>) { return installedPluginKey(plugin) }
function openEntryDetails(key: string) {
  if (!libraryEntries.value.some(entry => entry.key === key)) return
  closePluginMenu()
  detailKey.value = key
  showDetails.value = true
}
function closeEntryDetails() { if (!showDetails.value) detailKey.value = '' }
function selectDetailAction(action: 'update' | 'refetch') {
  if (!detailPlugin.value) return
  showDetails.value = false
  selectMore(action, detailPlugin.value)
}
function entryCategories(plugin?: InstalledPlugin, offer?: CatalogPlugin) {
  const values = plugin ? pluginToolCategories(plugin) : []
  return values.some(Boolean) ? values : [normalizePluginCategory(offer?.category)]
}
function entryDescription(plugin?: InstalledPlugin, offer?: CatalogPlugin) {
  return (plugin && pluginDescription(plugin)) || (offer && (pluginLocale(offer.locales, locale.value).description || offer.description)) || ''
}
async function refreshLibrary(verify = false) {
  if (disabled.value) return
  recentInstalls.value = new Map()
  if (verify) await refreshInventory(true)
  else await openMarketplace(true)
}
function libraryMenuAction(key: string) { void refreshLibrary(key === 'verify') }
function forgetRecentInstall(key: string) { const recent = new Map(recentInstalls.value); recent.delete(key); recentInstalls.value = recent }
async function restoreBundled(name: string, key: string) {
  const entry = libraryEntries.value.find(item => item.key === key && !item.installed)
  if (entry) recentInstalls.value = new Map(recentInstalls.value).set(key, catalogTitle(entry.catalog!))
  if (!await perform('install_preinstalled', name, undefined, { expectedRuntime: snapshot.value?.runtime.identity })) {
    forgetRecentInstall(key)
  }
}
function canRepairPlugin(plugin: InstalledPlugin) {
  return plugin.managed && plugin.enabled !== false && plugin.prepared !== false && !plugin.removalPending && !incompatible(plugin)
    && !!(plugin.error || plugin.dependencyError) && !canOpenPluginView(plugin)
}
function closePluginMenu(restoreFocus = false) {
  const trigger = pluginMenu.value?.trigger
  pluginMenu.value = null
  if (restoreFocus && trigger?.isConnected) trigger.focus({ preventScroll: true })
}
function closePluginMenuOnScroll(event: Event) {
  const target = event.target
  if (target instanceof Node && pageElement.value
    && (target.contains(pageElement.value) || pageElement.value.contains(target))) closePluginMenu()
}
function closePluginMenuOnResize() { closePluginMenu() }
function closePluginMenuOnKeydown(event: KeyboardEvent) {
  if (!pluginMenu.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    closePluginMenu(true)
  } else if (event.key === 'Tab') closePluginMenu()
}
function showPluginMenu(plugin: InstalledPlugin, trigger: HTMLElement, x: number, y: number, mode: 'actions' | 'views' = 'actions') {
  const runtime = snapshot.value?.runtime
  if (!runtime) return
  pluginMenu.value = { key: pluginKey(plugin), runtime: runtime.identity, python: runtime.pythonPath, x, y, trigger, mode }
}
function openPluginContextMenu(event: MouseEvent, plugin: InstalledPlugin) {
  if (!moreOptions(plugin).length) return
  if ((event.target as HTMLElement)?.closest('input, textarea, select, [contenteditable="true"]')) return
  event.preventDefault()
  event.stopPropagation()
  showPluginMenu(plugin, event.currentTarget as HTMLElement, event.clientX, event.clientY)
}
function canOpenPluginView(plugin: InstalledPlugin) {
  return plugin.loaded === true && plugin.enabled !== false && plugin.prepared !== false && !plugin.removalPending && !incompatible(plugin) && !busy.value
}
function pluginViewOptions(plugin: InstalledPlugin): DropdownOption[] {
  return (plugin.contributions?.views || []).map(view => ({
    label: pluginViewTitle(plugin, view, locale.value), key: `view:${view.id}`,
    icon: menuIcon(OpenOutline), disabled: !canOpenPluginView(plugin),
  }))
}
function openPluginViews(event: MouseEvent, plugin: InstalledPlugin) {
  if (!canOpenPluginView(plugin)) return
  const views = plugin.contributions?.views || []
  if (views.length === 1) { closePluginMenu(); openPluginView(plugin, views[0]); return }
  if (!views.length) return
  const trigger = event.currentTarget as HTMLElement
  if (pluginMenu.value?.trigger === trigger) { closePluginMenu(true); return }
  const bounds = trigger.getBoundingClientRect()
  showPluginMenu(plugin, trigger, bounds.left, bounds.bottom + 4, 'views')
}
function openPluginKeyboardMenu(event: KeyboardEvent, plugin: InstalledPlugin) {
  if (event.target !== event.currentTarget || !(event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10'))) return
  if (!moreOptions(plugin).length) return
  event.preventDefault()
  const trigger = event.currentTarget as HTMLElement
  const bounds = trigger.getBoundingClientRect()
  showPluginMenu(plugin, trigger, bounds.left + 40, bounds.bottom)
}
const menuPlugin = computed(() => {
  const menu = pluginMenu.value, runtime = snapshot.value?.runtime
  if (!menu || runtime?.identity !== menu.runtime || runtime.pythonPath !== menu.python) return undefined
  return installed.value.find(plugin => pluginKey(plugin) === menu.key)
})
const pluginMenuOptions = computed(() => menuPlugin.value
  ? pluginMenu.value?.mode === 'views' ? pluginViewOptions(menuPlugin.value) : moreOptions(menuPlugin.value)
  : [])
function selectPluginMenu(key: string | number) {
  const plugin = menuPlugin.value
  const option = pluginMenuOptions.value.find(item => item.key === key)
  if (!plugin || !option || ('disabled' in option && option.disabled)) { closePluginMenu(); return }
  closePluginMenu()
  if (key === 'open_directory') return openPluginDirectory(plugin)
  if (String(key).startsWith('view:')) {
    const view = plugin.contributions?.views?.find(item => `view:${item.id}` === key)
    if (view) openPluginView(plugin, view)
  } else selectMore(String(key), plugin)
}
function menuIcon(component: Component) { return () => h(NIcon, { component }) }
async function openPluginDirectory(plugin: InstalledPlugin) {
  if (!plugin.path) return
  try { await invoke('reveal_path', { path: plugin.path }) }
  catch (failure) { message.error(failure instanceof Error ? failure.message : String(failure)) }
}
function moreOptions(plugin: InstalledPlugin): (DropdownOption | DropdownDividerOption)[] {
  const location: (DropdownOption | DropdownDividerOption)[] = plugin.path
    ? [{ label: t('plugins.openDirectory'), key: 'open_directory', icon: menuIcon(FolderOpenOutline), disabled: plugin.prepared === false || plugin.removalPending }]
    : []
  if (!plugin.managed) return location
  return [
    ...location,
    ...(location.length ? [{ type: 'divider' as const, key: 'directory-divider' }] : []),
    { label: t(plugin.enabled === false ? 'plugins.enable' : 'plugins.disable'), key: plugin.enabled === false ? 'enable' : 'disable', icon: menuIcon(PowerOutline), disabled: disabled.value || plugin.removalPending || (plugin.enabled === false && (incompatible(plugin) || plugin.prepared === false)) },
    { label: t('plugins.repair'), key: 'repair', icon: menuIcon(BuildOutline), disabled: disabled.value || plugin.prepared === false || plugin.removalPending },
    { type: 'divider', key: 'divider' },
    { label: () => h('span', { style: { color: 'var(--danger)' } }, t('plugins.uninstall')), key: 'uninstall', icon: menuIcon(TrashOutline), disabled: disabled.value || (plugin.removalPending && plugin.cleanupRuntime !== snapshot.value?.runtime.identity) },
  ]
}
function selectMore(key: string, plugin: { name: string }) {
  if (key === 'update' || key === 'refetch') {
    const installed = snapshot.value?.installed.find(item => item.managed && item.name === plugin.name)
    const checked = updates.value[plugin.name]
    if (!installed || !snapshot.value || !canCheckPluginUpdate(installed) || (key === 'update' && !canApplyPluginUpdate(checked))) return
    updateName.value = plugin.name
    updateRuntime.value = snapshot.value?.runtime.identity || ''
    updateInstallation.value = pluginUpdateKey(installed, snapshot.value.runtime)
    updateMode.value = key
    reviewedUpdate.value = key === 'update' ? JSON.parse(JSON.stringify(checked)) : null
    installDependencies.value = false
    showUpdate.value = true
  } else if (key === 'repair') {
    const expectedRuntime = snapshot.value?.runtime.identity
    let confirmed = false
    dialog.warning({
      title: t('plugins.repair'), content: t('plugins.repairConfirm'),
      positiveText: t('plugins.repair'), negativeText: t('common.cancel'),
      onPositiveClick: () => {
        confirmed = true
        void perform('repair', plugin.name, undefined, { expectedRuntime })
      },
      onAfterLeave: () => { if (confirmed) openPluginLogs(pluginActivity(plugin.name)?.id) },
    })
  } else if (key === 'uninstall') uninstall(plugin.name)
  else if (key === 'install_preinstalled') void perform('install_preinstalled', plugin.name)
  else if (key === 'enable' || key === 'disable') void perform(key, plugin.name)
}

async function prepareRuntime(plugin: InstalledPlugin) {
  const source = plugin.sourceSelectionRequired ? plugin.availableBindings?.find(item => item.runtime === preparationSources.value[plugin.name]) : undefined
  if (plugin.sourceSelectionRequired && !source) return
  try {
    await manage('prepare', {
      name: plugin.name, expectedRuntime: snapshot.value?.runtime.identity,
      ...(source ? { sourceRuntime: source.runtime, sourceVersion: source.version, sourceContent: source.content } : {}),
    })
  } catch { /* The shared activity keeps the actionable failure details. */ }
}

async function enableDefaultTools() {
  settings.pluginDefaultsPolicy = 'auto'
  await perform('prepare_preinstalled')
}

async function confirmUpdate() {
  if (disabled.value || !updateName.value) return
  const name = updateName.value, dependencies = installDependencies.value
  if (!updateRuntime.value || snapshot.value?.runtime.identity !== updateRuntime.value) {
    showUpdate.value = false
    message.warning(t('plugins.updateRuntimeChanged'))
    return
  }
  const plugin = updatePlugin.value
  if (!plugin || !snapshot.value || !canCheckPluginUpdate(plugin) || pluginUpdateKey(plugin, snapshot.value.runtime) !== updateInstallation.value) {
    showUpdate.value = false
    message.warning(t('plugins.updateInstallationChanged'))
    return
  }
  showUpdate.value = false
  await perform('update', name, undefined, { installDependencies: dependencies, expectedRuntime: updateRuntime.value,
    ...(reviewedUpdate.value ? { expectedUpdate: reviewedUpdate.value } : {}),
  })
}

async function requestCancel(id?: string) {
  try { await cancel(id) }
  catch (failure) { message.error(String(failure)) }
}
</script>

<template>
  <div ref="pageElement" class="page plugins-page">
    <div class="page-header-compact plugins-header">
      <div><h1>{{ t('plugins.title') }}</h1><p>{{ t('plugins.subtitle') }}</p></div>
      <div class="plugins-header-controls">
        <div class="plugins-actions">

          <n-button v-if="snapshot" :loading="!!updateCheck" :disabled="disabled || !snapshot.installed.some(canCheckPluginUpdate)" @click="perform('check_updates', undefined, undefined, { expectedRuntime: snapshot.runtime.identity })">
            {{ t('plugins.checkUpdates') }}<template v-if="updateCount"> ({{ updateCount }})</template>
          </n-button>
          <n-button :disabled="!activity && !logs.length" :aria-label="activeTasks.length ? `${t('tasks.logs')} · ${t('plugins.runningTasks', { count: activeTasks.length })}` : t('tasks.logs')" @click="openPluginLogs()"><template #icon><n-spin v-if="activeTasks.length" :size="14" aria-hidden="true" /><n-icon v-else :component="TerminalOutline" /></template>{{ t('tasks.logs') }}<span v-if="activeTasks.length" class="plugins-running-count" role="status" aria-live="polite">{{ t('plugins.runningTasks', { count: activeTasks.length }) }}</span></n-button>
          <n-button :disabled="installDisabled() || !snapshot" @click="showSource = true">{{ t('plugins.installSource') }}</n-button>
          <n-dropdown v-if="snapshot" trigger="click" :options="[{ label: t('plugins.refreshCatalog'), key: 'refresh', icon: menuIcon(RefreshOutline), disabled }, { label: t('plugins.reverify'), key: 'verify', icon: menuIcon(CheckmarkCircleOutline), disabled }]" @select="libraryMenuAction"><n-button secondary :loading="loading" :disabled="disabled" :aria-label="t('plugins.moreMarketActions')"><template #icon><n-icon :component="EllipsisHorizontalOutline" /></template></n-button></n-dropdown>
        </div>
        <div v-if="snapshot" class="plugins-update-summary" aria-live="polite">
          <span :title="lastUpdateCheck ? t('plugins.updateCheckedAt', { time: lastUpdateCheck }) : undefined">{{ updateCheck ? t('plugins.updateChecking') : lastUpdateCheck ? t('plugins.updateCheckedAt', { time: lastUpdateCheck }) : t('plugins.updateUnchecked') }}</span>
          <span v-if="updateErrorCount" class="plugin-error">{{ t('plugins.updateErrorCount', { count: updateErrorCount }) }}</span>
        </div>
      </div>
    </div>

    <n-alert v-if="error" type="error" class="plugins-notice">{{ error }}</n-alert>
    <n-alert v-if="!snapshot && !loading && /No active runtime/i.test(error)" type="info" class="plugins-notice">
      {{ t('plugins.runtimeRequired') }}
      <router-link to="/settings">{{ t('nav.settings') }}</router-link>
    </n-alert>
    <template v-if="snapshot">
      <n-alert v-if="snapshot.runtime.managementSupported === false" type="warning" class="plugins-notice">{{ t('plugins.runtimeIncompatible') }}</n-alert>
      <n-alert v-else-if="snapshot.inventoryWarnings?.length" type="warning" class="plugins-notice">{{ t('plugins.inventoryMigrationWarning') }}</n-alert>
      <n-alert v-if="snapshot.preinstalledAvailable?.length" type="info" class="plugins-notice">
        <div class="plugins-default-tools">
          <span>{{ t('plugins.defaultToolsAvailable') }}</span>
          <n-button secondary size="small" :loading="activity?.action === 'prepare_preinstalled' && busy" :disabled="disabled" @click="enableDefaultTools">{{ t('plugins.enableDefaultTools') }}</n-button>
        </div>
      </n-alert>
      <n-alert v-if="snapshot.verificationError" type="warning" class="plugins-notice">{{ t('plugins.verificationUnavailable') }} {{ snapshot.verificationError }}</n-alert>
      <n-alert v-if="snapshot.legacyPlugins?.length" type="info" class="plugins-notice">{{ t('plugins.legacyPluginsMoved', { names: snapshot.legacyPlugins.join(', ') }) }}</n-alert>
      <div class="plugins-toolbar">
        <n-input v-model:value="search" clearable :placeholder="t('plugins.search')" class="plugins-search">
          <template #prefix><n-icon :component="SearchOutline" /></template>
        </n-input>
        <n-select v-model:value="category" :options="categoryOptions" :aria-label="t('plugins.categoryFilter')" class="plugins-category-filter" />
        <n-select v-model:value="statusFilter" :options="statusOptions" :aria-label="t('plugins.statusFilter')" class="plugins-status-filter" />
        <div class="plugins-view-toggle" role="group" :aria-label="t('plugins.viewMode')">
          <button type="button" :class="{ active: !cardView }" :aria-pressed="!cardView" :aria-label="t('plugins.viewList')" :title="t('plugins.viewList')" @click="settings.pluginViewMode = 'list'"><n-icon :component="ListOutline" /></button>
          <button type="button" :class="{ active: cardView }" :aria-pressed="cardView" :aria-label="t('plugins.viewCard')" :title="t('plugins.viewCard')" @click="settings.pluginViewMode = 'card'"><n-icon :component="GridOutline" /></button>
        </div>
        <span class="plugins-result-count" aria-live="polite">{{ visibleEntries.length }} / {{ libraryEntries.length }}</span>
      </div>
      <div class="plugins-library-bar">
        <n-popover trigger="click" placement="bottom-start">
          <template #trigger>
            <button type="button" class="plugins-runtime" :title="snapshot.runtime.pythonPath" :aria-label="t('plugins.librarySettings')">
              <n-icon :component="TerminalOutline" /><span>{{ t('plugins.librarySettings') }}</span><n-icon :component="ChevronDownOutline" />
            </button>
          </template>
          <div class="plugins-runtime-details">
            <strong>{{ t('plugins.runtimeScope') }}</strong><code>{{ snapshot.runtime.pythonPath }}</code>
            <strong class="plugins-effective-source">{{ sourceStatus }}</strong>
            <strong>{{ t('plugins.sourceLabel') }}</strong>
            <n-select :value="settings.pluginSource || 'auto'" :options="sourceOptions" :aria-label="t('plugins.sourceLabel')"
              :disabled="busy || loading || catalogLoading || switchingSource || snapshot.catalogSource?.supported === false" @update:value="changePluginSource" />
            <span v-if="customSource" class="plugins-source-status">{{ t('plugins.sourceCustom') }}</span>
            <span v-else-if="!settings.pluginSource || settings.pluginSource === 'auto'" class="plugins-source-status">{{ t('plugins.sourceAutoHint') }}</span>
            <span v-if="snapshot.catalogSource?.gitAvailable === false" class="plugins-source-status">{{ t('plugins.sourceGitRequired') }}</span>
          </div>
        </n-popover>
        <span class="plugins-library-hint">{{ t('plugins.listHint') }}</span>
      </div>
      <n-alert v-if="snapshot.catalogSource?.supported === false" type="warning" class="plugins-notice">{{ t('plugins.sourceCoreUpdate') }}</n-alert>
      <n-alert v-if="snapshot.catalogError" type="warning" class="plugins-notice">
        <div class="plugins-catalog-error"><span>{{ t('plugins.catalogUnavailable') }} {{ snapshot.catalogError }}</span><n-button size="small" secondary :loading="catalogLoading" :disabled="disabled" @click="refreshLibrary()">{{ t('plugins.refreshCatalog') }}</n-button></div>
      </n-alert>
      <div v-if="visibleEntries.length" class="plugins-installed" :class="{ 'plugins-installed--card': cardView }" role="list">
        <article v-for="{ installed: plugin, catalog: offer, key } in visibleEntries" :key="key" class="plugin-card plugin-library-entry" role="listitem" tabindex="-1"
          :aria-label="plugin ? pluginTitle(plugin) : offer ? catalogTitle(offer) : key"
          :class="{ 'plugin-installed-card': !!plugin, 'plugin-available-card': !plugin, 'plugin-installed-card--active': pluginMenu?.key === key, 'plugin-library-entry--installed-now': !!plugin && recentInstalls.has(key) }"
          @click.self="openEntryDetails(key)"
          @contextmenu="plugin && openPluginContextMenu($event, plugin)" @keydown="plugin && openPluginKeyboardMenu($event, plugin)">
          <template v-if="plugin">
            <div class="plugin-installed-summary">
              <div role="button" tabindex="0" class="plugin-installed-toggle"
                :aria-label="pluginTitle(plugin)" aria-haspopup="dialog" aria-controls="plugin-detail-dialog"
                :aria-describedby="entryDescription(plugin, offer) ? `plugin-description-${pluginKey(plugin)}` : undefined"
                @click="openEntryDetails(key)" @keydown.enter.self.prevent="openEntryDetails(key)" @keydown.space.self.prevent @keyup.space.self.prevent="openEntryDetails(key)"
                @keydown="openPluginKeyboardMenu($event, plugin)">
                <div class="plugin-installed-icon"><n-icon :component="ExtensionPuzzleOutline" :size="22" /></div>
                <div class="plugin-installed-main">
                  <div class="plugin-installed-header">
                    <h2>{{ pluginTitle(plugin) }}</h2>
                    <n-tag v-if="!plugin.managed" size="small" :bordered="false">{{ t('plugins.readOnly') }}</n-tag>
                    <n-tag v-else-if="!offer && !plugin.preinstalled && plugin.source === 'path'" size="small" :bordered="false">{{ t('plugins.localInstallation') }}</n-tag>
                    <n-tag v-if="recentInstalls.has(key)" size="small" type="success" :bordered="false">{{ t('plugins.installComplete') }}</n-tag>
                    <n-tag v-if="plugin.preinstalled" size="small">{{ t('plugins.preinstalled') }}</n-tag>
                    <n-tag v-if="incompatible(plugin)" type="warning" size="small">{{ t('plugins.incompatible') }}</n-tag>
                    <n-tag v-else-if="plugin.removalPending" type="warning" size="small">{{ t('plugins.removalPending') }}</n-tag>
                    <n-tag v-else-if="plugin.prepared === false" type="warning" size="small">{{ t('plugins.notPrepared') }}</n-tag>
                    <n-tag v-else :type="plugin.enabled === false ? 'default' : plugin.loaded == null ? 'warning' : plugin.loaded ? 'success' : 'error'" size="small">{{ plugin.enabled === false ? t('plugins.disabled') : plugin.loaded == null ? t('plugins.notVerified') : plugin.loaded ? t('plugins.loaded') : t('plugins.loadFailed') }}</n-tag>
                    <n-tag v-if="canApplyPluginUpdate(pluginUpdate(plugin))" type="info" size="small">{{ t(pluginUpdateState(pluginUpdate(plugin)) === 'content' ? 'plugins.updateContentReady' : 'plugins.updateReady') }}</n-tag>
                    <n-tag v-else-if="pluginUpdateState(pluginUpdate(plugin)) === 'error'" type="warning" size="small">{{ t('plugins.updateCheckFailed') }}</n-tag>
                  </div>
                  <div class="plugin-installed-metadata">
                    <span class="plugin-id">{{ plugin.name }}<span v-if="plugin.version"> · {{ plugin.version }}</span></span>
                    <div class="plugin-category-list"><n-tag v-for="value in entryCategories(plugin, offer)" :key="value || 'other'" size="small" :bordered="false">{{ categoryLabel(value) }}</n-tag></div>
                  </div>
                </div>
                <p v-if="entryDescription(plugin, offer)" :id="`plugin-description-${pluginKey(plugin)}`" class="plugin-installed-description" :title="entryDescription(plugin, offer)">{{ entryDescription(plugin, offer) }}</p>
              </div>
              <div class="plugin-installed-actions">
                <n-button v-if="activePluginTask(plugin)" secondary size="small" class="plugin-running-task"
                  :title="t(`plugins.actions.${activePluginTask(plugin)!.action}`)" @click="openPluginLogs(activePluginTask(plugin)?.id)">
                  <template #icon><n-spin :size="14" /></template>
                  <span role="status" aria-live="polite">{{ t(activePluginTask(plugin)!.status === 'cancelling' ? 'plugins.taskStates.cancelling' : `plugins.phases.${activePluginTask(plugin)!.phase}`) }}</span>
                </n-button>
                <n-button v-else-if="plugin.prepared === false && plugin.availableBindings?.length && !plugin.removalPending" type="primary" size="small"
                  :loading="installing(plugin.name)" :disabled="disabled || incompatible(plugin) || (plugin.sourceSelectionRequired && !plugin.availableBindings.some(item => item.runtime === preparationSources[plugin.name]))"
                  @click="prepareRuntime(plugin)">{{ t('plugins.prepareRuntime') }}</n-button>
                <n-button v-else-if="plugin.preinstalled && plugin.prepared === false && !plugin.removalPending" type="primary" size="small" :loading="installing(plugin.name)" :disabled="disabled"
                  @click="restoreBundled(plugin.name, key)">{{ t('plugins.restorePreinstalled') }}</n-button>
                <n-button v-else-if="canRepairPlugin(plugin)" size="small" secondary :disabled="disabled" @click="selectMore('repair', plugin)">{{ t('plugins.repair') }}</n-button>
                <n-button v-else-if="plugin.contributions?.views?.length" type="primary" secondary size="small" :disabled="!canOpenPluginView(plugin)"
                  :aria-haspopup="plugin.contributions.views.length > 1 ? 'menu' : undefined"
                  :aria-expanded="plugin.contributions.views.length > 1 ? pluginMenu?.key === pluginKey(plugin) && pluginMenu.mode === 'views' : undefined"
                  @click="openPluginViews($event, plugin)">
                  {{ t('plugins.openTool') }}<n-icon v-if="plugin.contributions.views.length > 1" :component="ChevronDownOutline" class="plugin-open-chevron" />
                </n-button>
                <span v-else class="plugin-primary-state" :title="plugin.viewError || (plugin.loaded ? t('plugins.noToolPage') : undefined)">{{ t(plugin.removalPending ? 'plugins.removalPending' : plugin.enabled === false ? 'plugins.disabled' : plugin.prepared === false ? 'plugins.notPrepared' : plugin.loaded == null ? 'plugins.notVerified' : plugin.loaded ? 'plugins.enabled' : 'plugins.loadFailed') }}</span>
              </div>
            </div>
            <p v-if="plugin.removalPending" class="plugins-help">{{ t('plugins.removalPendingHint') }}</p>
            <template v-else-if="plugin.prepared === false">
              <p class="plugins-help">{{ t(plugin.enabled === false ? 'plugins.prepareDisabledHint' : 'plugins.prepareRuntimeHint') }}</p>
              <n-select v-if="plugin.sourceSelectionRequired" v-model:value="preparationSources[plugin.name]" :disabled="disabled"
                :placeholder="t('plugins.chooseSourceVersion')"
                :options="(plugin.availableBindings || []).map(item => ({ value: item.runtime, label: `${item.runtime} · ${item.version || '—'}` }))" />
              <p class="plugins-help">{{ t(plugin.availableBindings?.length ? 'plugins.prepareDependenciesHint' : 'plugins.prepareSourceMissing') }}</p>
            </template>
            <p v-if="plugin.error && !incompatible(plugin)" class="plugin-error">{{ plugin.error }}</p>
            <p v-if="plugin.viewError" class="plugins-help">{{ t('plugins.viewUnavailable') }} {{ plugin.viewError }}</p>
            <p v-if="incompatible(plugin)" class="plugin-error">{{ plugin.compatibility?.reasons.join('\n') }}</p>
            <p v-if="plugin.dependencyError" class="plugins-help" :title="plugin.dependencyError">{{ t('plugins.optionalBackendHint') }}</p>
            <p v-if="!plugin.managed" class="plugins-help">{{ t('plugins.manuallyInstalled') }}</p>

          </template>
          <template v-else-if="offer">
            <div class="plugin-installed-summary">
              <div role="button" tabindex="0" class="plugin-installed-toggle"
                :aria-label="catalogTitle(offer)" aria-haspopup="dialog" aria-controls="plugin-detail-dialog"
                @click="openEntryDetails(key)" @keydown.enter.self.prevent="openEntryDetails(key)" @keydown.space.self.prevent @keyup.space.self.prevent="openEntryDetails(key)">
                <div class="plugin-installed-icon"><n-icon :component="ExtensionPuzzleOutline" :size="22" /></div>
                <div class="plugin-installed-main">
                  <div class="plugin-installed-header"><h2>{{ catalogTitle(offer) }}</h2><n-tag v-if="incompatible(offer)" size="small" type="warning">{{ t('plugins.incompatible') }}</n-tag><n-tag v-else size="small" :bordered="false">{{ t('plugins.notInstalled') }}</n-tag><n-tag v-if="bundledOffers.has(offer.name)" size="small">{{ t('plugins.preinstalled') }}</n-tag></div>
                  <div class="plugin-installed-metadata"><span class="plugin-id">{{ offer.name }}<span v-if="offer.version"> · {{ offer.version }}</span></span><div class="plugin-category-list"><n-tag v-for="value in entryCategories(undefined, offer)" :key="value || 'other'" size="small" :bordered="false">{{ categoryLabel(value) }}</n-tag></div></div>
                </div>
                <p v-if="entryDescription(undefined, offer)" class="plugin-installed-description" :title="entryDescription(undefined, offer)">{{ entryDescription(undefined, offer) }}</p>
              </div>
              <div class="plugin-installed-actions">
                <n-button v-if="offer.url" type="primary" size="small" :loading="installing(offer.name)" :disabled="installDisabled(offer.name) || incompatible(offer)" @click="install(offer.name)">{{ installLabel(offer.name) }}</n-button>
                <n-button v-else-if="bundledOffers.has(offer.name)" type="primary" size="small" :disabled="installDisabled(offer.name)" :loading="installing(offer.name)" @click="restoreBundled(offer.name, key)">{{ t('plugins.restorePreinstalled') }}</n-button>
              </div>
            </div>
            <p v-if="pluginActivity(offer.name)?.status === 'failed'" class="plugin-error">{{ pluginActivity(offer.name)?.error }}</p>

          </template>
        </article>
      </div>
      <div v-if="catalogLoading && statusFilter !== 'installed' && statusFilter !== 'attention'" class="plugins-list-skeleton" :aria-label="t('plugins.catalogLoading')"><n-skeleton v-for="index in 3" :key="index" height="88px" :sharp="false" /></div>
      <n-empty v-else-if="!visibleEntries.length" size="small" :description="t('plugins.noMatches')" class="plugins-empty" />

    </template>
    <n-spin v-if="loading && !snapshot" class="plugins-empty" />

    <n-dropdown v-if="menuPlugin && pluginMenu" trigger="manual" placement="bottom-start" :show="true" :x="pluginMenu.x" :y="pluginMenu.y" :options="pluginMenuOptions"
      :menu-props="() => ({ role: 'menu', 'aria-label': pluginMenu?.mode === 'views' ? t('plugins.openTool') : t('plugins.more') })"
      :node-props="(option: DropdownOption) => ({ role: 'menuitem', tabindex: -1, 'aria-disabled': !!option.disabled })"
      @clickoutside="closePluginMenu()"
      @select="selectPluginMenu" @update:show="(value: boolean) => { if (!value) closePluginMenu(true) }" />

    <n-modal v-model:show="showDetails" preset="card" id="plugin-detail-dialog" role="dialog" aria-modal="true"
      :title="detailPlugin ? pluginTitle(detailPlugin) : detailOffer ? catalogTitle(detailOffer) : t('plugins.detail')"
      class="plugin-detail-modal" style="width: min(620px, calc(100vw - 32px))" @after-leave="closeEntryDetails">
      <div v-if="detailEntry" class="plugin-modal-body">
        <div class="plugin-detail-identity">
          <span class="plugin-id">{{ detailEntry.name }}<span v-if="detailPlugin ? detailPlugin.version : detailOffer?.version"> · {{ detailPlugin ? detailPlugin.version : detailOffer?.version }}</span></span>
          <n-tag v-if="detailPlugin && !detailPlugin.managed" size="small" :bordered="false">{{ t('plugins.readOnly') }}</n-tag>
        </div>
        <p v-if="entryDescription(detailPlugin, detailOffer)" class="plugin-detail-description">{{ entryDescription(detailPlugin, detailOffer) }}</p>
        <div v-if="detailPlugin" class="plugin-modal-details">
          <p v-if="detailEntry?.otherSources.length" class="plugins-help">{{ t('plugins.sourceConflictHint') }}</p>
          <div class="plugin-detail-meta">
            <span v-if="detailPlugin.operations?.length">{{ t('plugins.operations') }} · {{ detailPlugin.operations.length }}</span>
            <span v-if="detailPlugin.resolvedCommit">{{ t('plugins.sourceCommit') }} <code :title="detailPlugin.resolvedCommit">{{ detailPlugin.resolvedCommit.slice(0, 12) }}</code></span>
            <span v-if="checkingUpdate(detailPlugin) || (pluginUpdate(detailPlugin) && pluginUpdateState(pluginUpdate(detailPlugin)) !== 'error')">{{ updateStatusText(detailPlugin) }}</span>
            <span v-if="checkedTime(pluginUpdate(detailPlugin))">{{ t('plugins.updateCheckedAt', { time: checkedTime(pluginUpdate(detailPlugin)) }) }}</span>
          </div>
          <p v-if="pluginUpdate(detailPlugin)?.error" class="plugin-error">{{ pluginUpdate(detailPlugin)?.error }}</p>
          <div v-if="detailPlugin.operations?.length" class="plugin-operations">
            <ul class="plugin-operation-list"><li v-for="operation in operationLabels(detailPlugin)" :key="operation.id">{{ operation.title }}</li></ul>
          </div>
          <p v-else-if="detailPlugin.loaded && detailPlugin.managed && !detailPlugin.contributions?.views?.length" class="plugins-help">{{ t('plugins.noOperations') }}</p>
          <div v-if="detailPlugin.managed" class="plugin-details-actions">
            <n-button v-if="canApplyPluginUpdate(pluginUpdate(detailPlugin))" type="primary" size="small" :disabled="disabled || !canCheckPluginUpdate(detailPlugin)"
              @click="selectDetailAction('update')">{{ t(pluginUpdateState(pluginUpdate(detailPlugin)) === 'content' ? 'plugins.syncSourceContent' : 'plugins.update') }}</n-button>
            <n-button v-if="pluginUpdateState(pluginUpdate(detailPlugin)) === 'error' && canCheckPluginUpdate(detailPlugin)" size="small" secondary :disabled="disabled"
              @click="perform('check_updates', detailPlugin.name, undefined, { expectedRuntime: snapshot?.runtime.identity })">{{ t('plugins.retryUpdateCheck') }}</n-button>
            <n-button v-if="canCheckPluginUpdate(detailPlugin)" size="small" text :disabled="disabled" @click="selectDetailAction('refetch')">{{ t('plugins.refetch') }}</n-button>
          </div>
        </div>
        <div v-else-if="detailOffer" class="plugin-modal-details">
          <p v-if="incompatible(detailOffer)" class="plugin-error">{{ detailOffer.compatibility?.reasons.join('\n') }}</p>
          <div class="plugin-tags"><n-tag v-for="tag in (pluginLocale(detailOffer.locales, locale).tags || detailOffer.tags)" :key="tag" size="small" :bordered="false">{{ tag }}</n-tag></div>
          <ul v-if="Object.keys(pluginLocale(detailOffer.locales, locale).operations || {}).length" class="plugin-operation-list"><li v-for="(title, id) in pluginLocale(detailOffer.locales, locale).operations" :key="id">{{ title }}</li></ul>
          <p class="plugin-source" :title="detailOffer.url">{{ detailOffer.url ? sourceLabel(detailOffer.url) : t('plugins.bundledSource') }}</p>
          <div v-if="detailOffer.url && bundledOffers.has(detailOffer.name)" class="plugin-details-actions"><n-button size="small" text :disabled="installDisabled(detailOffer.name)" @click="restoreBundled(detailOffer.name, detailKey)">{{ t('plugins.restorePreinstalled') }}</n-button></div>
        </div>
      </div>
    </n-modal>

    <PluginLogDialog v-model:show="showLogs" :records="logRecords" :task="activity" :tasks="activities" :initial-task-id="logTaskId" @cancel="requestCancel" />

    <n-modal v-model:show="showUpdate" preset="card" :title="t(updateMode === 'refetch' ? 'plugins.refetchTitle' : 'plugins.updateTitle', { name: updatePlugin ? pluginTitle(updatePlugin) : updateName })" class="plugin-source-modal plugin-update-modal" style="width: min(520px, calc(100vw - 32px))">
      <p class="plugin-update-description">{{ t(updateMode === 'refetch' ? 'plugins.refetchDescription' : 'plugins.updateDescription') }}</p>
      <div v-if="reviewedUpdate" class="plugins-update-versions">
        <div><span>{{ t('plugins.currentVersion') }}</span><strong>{{ updatePlugin?.version || t('plugins.unknownVersion') }}</strong></div>
        <n-icon v-if="reviewedUpdate.remote_version !== updatePlugin?.version" :component="ArrowForwardOutline" aria-hidden="true" />
        <div v-if="reviewedUpdate.remote_version !== updatePlugin?.version"><span>{{ t('plugins.targetVersion') }}</span><strong>{{ reviewedUpdate.remote_version }}</strong></div>
        <n-tag v-else :bordered="false" type="info" size="small">{{ t('plugins.updateContentReady') }}</n-tag>
      </div>
      <p v-else class="plugin-update-note">{{ t('plugins.refetchVersionHint') }}</p>
      <p class="plugin-update-assurance"><n-icon :component="CheckmarkCircleOutline" />{{ t('plugins.updateFilesHint') }}</p>
      <details class="plugin-update-options">
        <summary><n-icon :component="ChevronDownOutline" />{{ t('plugins.updateOptions') }}</summary>
        <div class="plugin-update-options-body">
          <n-checkbox v-model:checked="installDependencies" :disabled="disabled">{{ t('plugins.updateDependencies') }}</n-checkbox>
          <p class="plugin-update-note" :class="{ 'plugin-update-download-note': installDependencies }">{{ t(installDependencies ? 'plugins.updateDependenciesHint' : 'plugins.updateDependenciesHelp') }}</p>
          <p v-if="updateMode === 'refetch'" class="plugin-id">{{ t('plugins.currentVersion') }} <code>{{ updatePlugin?.version || t('plugins.unknownVersion') }}</code></p>
          <p v-if="reviewedUpdate?.remote_commit" class="plugin-id">{{ t('plugins.sourceCommit') }} <code>{{ reviewedUpdate.remote_commit }}</code></p>
        </div>
      </details>
      <template #footer><div class="plugin-update-footer"><n-button @click="showUpdate = false">{{ t('common.cancel') }}</n-button><n-button type="primary" :disabled="disabled" @click="confirmUpdate">{{ t(updateMode === 'refetch' ? 'plugins.refetch' : reviewedUpdate?.status === 'content' ? 'plugins.syncSourceContent' : 'plugins.update') }}</n-button></div></template>
    </n-modal>

    <n-modal v-model:show="showSource" preset="card" :title="t('plugins.installSource')" class="plugin-source-modal" style="width: min(560px, calc(100vw - 32px))">
      <p class="plugins-help">{{ t('plugins.sourceHint') }}</p>
      <n-input v-model:value="source" :placeholder="t('plugins.sourcePlaceholder')" :disabled="installDisabled()" />
      <template #footer><div class="plugins-actions">
        <n-button :disabled="disabled" @click="chooseFolder"><template #icon><n-icon :component="FolderOpenOutline" /></template>{{ t('plugins.chooseFolder') }}</n-button>
        <n-button type="primary" :disabled="installDisabled(source) || !source.trim()" @click="install(undefined, source)">{{ t('plugins.install') }}</n-button>
      </div></template>
    </n-modal>
  </div>
</template>

<style scoped>
.plugins-page { max-width: 1400px; margin: 0 auto; }
.plugins-header, .plugins-toolbar, .plugins-actions, .plugin-installed-header { display: flex; align-items: center; gap: 12px; }
.plugins-header, .plugin-installed-header { justify-content: space-between; }
.plugins-actions { flex-wrap: wrap; }
.plugins-header-controls { display: grid; justify-items: end; gap: 6px; min-width: 0; max-width: 100%; }
.plugins-header-controls .plugins-actions { justify-content: flex-end; gap: 8px; }
.plugins-header-controls .n-button { flex-shrink: 0; }
.plugins-running-count { margin-left: 6px; color: var(--on-surface-muted); font-size: 11px; }
.plugin-running-task { max-width: 240px; }
.plugins-notice { margin-bottom: 16px; }
.plugins-default-tools { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px 16px; }
.plugins-default-tools > span { flex: 1; min-width: min(100%, 280px); }
.plugins-default-tools > .n-button { flex-shrink: 0; }
.plugins-runtime { display: inline-flex; align-items: center; gap: 7px; max-width: 360px; padding: 5px 7px; border: 0; border-radius: 5px; background: transparent; color: var(--on-surface-muted); font: inherit; font-size: 11px; cursor: pointer; }
.plugins-runtime:hover { color: var(--on-surface); background: var(--surface-2); }
.plugins-runtime:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.plugins-runtime > span, .plugins-runtime > .n-icon { flex-shrink: 0; }
.plugins-runtime-details { display: grid; gap: 10px; width: min(360px, calc(100vw - 48px)); font-size: 12px; }
.plugins-runtime-details code { white-space: normal; overflow-wrap: anywhere; font: 11px/1.6 var(--font-mono); }
.plugins-library-bar { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px 16px; margin: 6px 0 16px; }
.plugins-library-hint { color: var(--on-surface-muted); font-size: 11px; }
.plugins-list-skeleton { display: grid; gap: 10px; margin-top: 10px; }
.plugins-catalog-error { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; }
.plugins-catalog-error > span { flex: 1; min-width: min(100%, 240px); }
.plugins-toolbar { flex-wrap: wrap; gap: 8px; margin: 4px 0 0; }
.plugins-search { flex: 1; min-width: 180px; }
.plugins-category-filter { width: 150px; flex-shrink: 0; }
.plugins-status-filter { width: 160px; flex-shrink: 0; }
.plugins-result-count { flex-shrink: 0; min-width: 48px; text-align: right; color: var(--on-surface-muted); font: 11px var(--font-mono); }
.plugins-view-toggle { display: inline-flex; flex-shrink: 0; gap: 2px; padding: 2px; border-radius: 8px; background: color-mix(in srgb, var(--surface-2) 60%, transparent); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--outline) 55%, transparent); }
.plugins-view-toggle button { display: grid; place-items: center; width: 28px; height: 26px; padding: 0; border: 0; border-radius: 6px; background: transparent; color: var(--on-surface-muted); cursor: pointer; font-size: 15px; transition: background 150ms ease, color 150ms ease; }
.plugins-view-toggle button:hover { color: var(--on-surface); }
.plugins-view-toggle button.active { background: var(--surface-1); color: var(--primary); box-shadow: 0 1px 2px rgba(0, 0, 0, .12); }
.plugins-view-toggle button:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.plugins-source-status { color: var(--on-surface-muted); font-size: 11px; }
.plugin-category-list { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
.plugin-card { border: 1px solid var(--outline); background: var(--surface-1); border-radius: 12px; padding: 20px; min-width: 0; }
.plugin-card h2 { font-size: 17px; margin: 0; }
.plugin-tags { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 12px; }
.plugin-source { font-size: 11px; opacity: .6; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.plugins-installed { display: grid; gap: 10px; }
.plugin-library-entry { padding: 14px 18px; }
.plugin-library-entry:hover, .plugin-installed-card--active { border-color: var(--primary-border); }
.plugin-library-entry:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.plugin-library-entry--installed-now { animation: plugin-installed-highlight 1.4s ease-out; }
@keyframes plugin-installed-highlight { from { background: var(--primary-soft); } to { background: var(--surface-1); } }
.plugin-installed-summary { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 12px; }
.plugin-installed-toggle { display: grid; grid-template-columns: 36px minmax(0, 1fr); align-items: center; gap: 6px 12px; min-width: 0; margin: -6px; padding: 6px; border: 0; border-radius: 6px; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.plugin-installed-toggle:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.plugin-installed-icon { display: grid; grid-row: 1 / span 2; place-items: center; width: 36px; height: 36px; border-radius: 9px; background: var(--primary-softer); color: var(--primary-strong); }
.plugin-installed-main { min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: 4px; }
.plugin-installed-header { justify-content: flex-start; flex-wrap: wrap; gap: 8px; }
.plugin-installed-header h2 { font-size: 15px; overflow-wrap: anywhere; }
.plugin-installed-metadata { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 12px; }
.plugin-installed-metadata .plugin-id, .plugin-installed-metadata .plugin-category-list { margin: 0; }
.plugin-installed-description { grid-column: 2; margin: 0; color: var(--on-surface-muted); font-size: 12px; line-height: 1.6; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
.plugin-installed-actions { display: flex; align-items: center; gap: 4px; }
.plugin-primary-state { color: var(--on-surface-muted); font-size: 12px; }
.plugin-open-chevron { margin-left: 7px; }
.plugin-library-entry > .plugins-help, .plugin-library-entry > .plugin-error { margin: 8px 0 0 48px; }
.plugin-installed-card > .n-select { max-width: 400px; margin: 8px 0 0 48px; }
.plugins-installed--card { grid-template-columns: repeat(auto-fill, minmax(min(100%, 280px), 1fr)); gap: 14px; }
.plugins-installed--card .plugin-library-entry { display: flex; flex-direction: column; min-height: 180px; padding: 18px; cursor: pointer; }
.plugins-installed--card .plugin-installed-summary { display: contents; }
.plugins-installed--card .plugin-installed-toggle { align-items: start; }
.plugins-installed--card .plugin-installed-icon { grid-row: 1; }
.plugins-installed--card .plugin-installed-description { grid-column: 1 / -1; margin-top: 4px; }
.plugins-installed--card .plugin-installed-actions { order: 2; justify-content: flex-end; margin: auto 0 0; padding-top: 16px; }
.plugins-installed--card .plugin-library-entry > .plugins-help, .plugins-installed--card .plugin-library-entry > .plugin-error { margin-left: 0; }
.plugins-installed--card .plugin-installed-card > .n-select { max-width: 100%; margin-left: 0; }
:global(.plugin-detail-modal .n-card-content), :global(.plugin-detail-modal .n-card__content) { scrollbar-gutter: stable; scrollbar-width: thin; scrollbar-color: color-mix(in srgb, var(--on-surface-muted) 35%, transparent) transparent; }
@supports selector(::-webkit-scrollbar) {
  :global(.plugin-detail-modal .n-card-content), :global(.plugin-detail-modal .n-card__content) { scrollbar-width: auto; scrollbar-color: auto; }
}
:global(.plugin-detail-modal .n-card-content::-webkit-scrollbar), :global(.plugin-detail-modal .n-card__content::-webkit-scrollbar) { width: 6px; }
:global(.plugin-detail-modal .n-card-content::-webkit-scrollbar-track), :global(.plugin-detail-modal .n-card__content::-webkit-scrollbar-track) { background: transparent; margin-block: 6px; }
:global(.plugin-detail-modal .n-card-content::-webkit-scrollbar-thumb), :global(.plugin-detail-modal .n-card__content::-webkit-scrollbar-thumb) { border-radius: 999px; background: color-mix(in srgb, var(--on-surface-muted) 35%, transparent); }
:global(.plugin-detail-modal .n-card-content::-webkit-scrollbar-thumb:hover), :global(.plugin-detail-modal .n-card__content::-webkit-scrollbar-thumb:hover) { background: color-mix(in srgb, var(--on-surface-muted) 55%, transparent); }
:global(.plugin-detail-modal .n-card-content::-webkit-scrollbar-button), :global(.plugin-detail-modal .n-card__content::-webkit-scrollbar-button), :global(.plugin-detail-modal .n-card-content::-webkit-scrollbar-corner), :global(.plugin-detail-modal .n-card__content::-webkit-scrollbar-corner) { display: none; width: 0; height: 0; }
.plugin-modal-body { display: grid; gap: 16px; overflow-wrap: anywhere; }
.plugin-detail-identity { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.plugin-detail-identity .plugin-id { margin: 0; }
.plugin-detail-description { margin: 0; color: var(--on-surface-muted); font-size: 13px; line-height: 1.7; }
.plugin-modal-details { border-top: 1px solid var(--outline); padding-top: 16px; }
.plugin-modal-details > :first-child { margin-top: 0; }
.plugin-details-actions { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 8px; }
.plugin-details-actions:not(:empty) { margin-top: 12px; }
.plugins-update-summary { display: flex; align-items: center; gap: 8px; min-width: 0; max-width: 240px; color: var(--on-surface-muted); font-size: 11px; }
.plugins-update-summary > :first-child { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.plugins-update-summary .plugin-error { margin: 0; flex-shrink: 0; white-space: nowrap; }
.plugin-detail-meta { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; margin-bottom: 6px; color: var(--on-surface-muted); font-size: 11px; line-height: 1.6; overflow-wrap: anywhere; }
.plugin-detail-meta:empty { display: none; }
.plugin-id { margin: 4px 0 10px; color: var(--on-surface-muted); font: 11px/1.4 var(--font-mono); overflow-wrap: anywhere; }
.plugin-operation-list { margin: 0; padding-left: 18px; font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.plugins-help { opacity: .65; font-size: 12px; }
.plugin-error { color: var(--danger); white-space: pre-wrap; overflow-wrap: anywhere; }
.plugins-empty { display: flex; justify-content: center; padding: 60px 20px; }
.plugin-update-description { margin: 0; color: var(--on-surface); font-size: 13px; line-height: 1.8; }
.plugins-update-versions { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 12px 20px; margin: 16px 0; }
.plugins-update-versions > div { display: grid; gap: 5px; min-width: 0; }
.plugins-update-versions span { color: var(--on-surface-muted); font-size: 11px; }
.plugins-update-versions strong { font: 600 17px/1.4 var(--font-mono); overflow-wrap: anywhere; }
.plugins-update-versions > .n-icon { margin-bottom: 4px; color: var(--on-surface-muted); font-size: 18px; }
.plugin-update-note { margin: 8px 0 0; color: var(--on-surface-muted); font-size: 12px; line-height: 1.7; }
.plugin-update-assurance { display: flex; align-items: flex-start; gap: 6px; margin: 16px 0 0; color: var(--on-surface-muted); font-size: 12px; line-height: 1.7; }
.plugin-update-assurance .n-icon { flex-shrink: 0; margin-top: 2px; color: var(--primary); font-size: 16px; }
.plugin-update-options { margin-top: 18px; border-top: 1px solid var(--outline); }
.plugin-update-options summary { display: flex; align-items: center; gap: 6px; padding: 12px 0 0; color: var(--on-surface-muted); font-size: 12px; cursor: pointer; list-style: none; }
.plugin-update-options summary::-webkit-details-marker { display: none; }
.plugin-update-options summary:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.plugin-update-options summary .n-icon { transition: transform 180ms ease; }
.plugin-update-options:not([open]) summary .n-icon { transform: rotate(-90deg); }
.plugin-update-options-body { padding-top: 14px; }
.plugin-update-options-body .plugin-id { margin-top: 14px; margin-bottom: 0; }
.plugin-update-download-note { color: var(--warning); }
.plugin-update-footer { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 8px; }
@media (max-width: 720px) {
  .plugins-header { align-items: flex-start; flex-direction: column; }
  .plugins-header-controls { justify-items: start; width: 100%; }
  .plugins-header-controls .plugins-actions { justify-content: flex-start; }
  .plugins-search { flex-basis: 100%; }
  .plugin-installed-summary { grid-template-columns: minmax(0, 1fr); gap: 10px; }
  .plugin-installed-toggle { grid-template-columns: 32px minmax(0, 1fr); gap: 10px; }
  .plugin-installed-icon { width: 32px; height: 32px; }
  .plugin-installed-actions { flex-wrap: wrap; margin-left: 42px; }
  .plugin-library-entry > .plugins-help, .plugin-library-entry > .plugin-error, .plugin-installed-card > .n-select { margin-left: 42px; }
  .plugin-installed-card > .n-select { max-width: calc(100% - 42px); }
}
</style>
