<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, useAttrs, useId, watch, type ComponentPublicInstance, type HTMLAttributes } from 'vue'
import { useI18n } from 'vue-i18n'
import { useSettingsStore } from '@/stores/settings'
import {
  ComboboxAnchor, ComboboxContent, ComboboxGroup, ComboboxLabel, ComboboxInput, ComboboxItem, ComboboxPortal,
  ComboboxRoot, ComboboxVirtualizer, SelectContent, SelectGroup, SelectItem,
  SelectItemText, SelectLabel, SelectPortal, SelectRoot, SelectTrigger, SelectValue, SelectViewport,
} from 'reka-ui'
import { appSelectEntries, appSelectKey, filterAppSelectEntries, type AppSelectEntry, type AppSelectOption, type AppSelectValue } from '@/utils/appSelect'

defineOptions({ inheritAttrs: false })
const props = withDefaults(defineProps<{
  value?: AppSelectValue
  options?: readonly AppSelectOption[]
  label?: string
  ariaLabel?: string
  ariaLabelledby?: string
  id?: string
  placeholder?: string
  disabled?: boolean
  loading?: boolean
  filterable?: boolean
  multiple?: boolean
  clearable?: boolean
  show?: boolean
  size?: 'tiny' | 'small' | 'medium' | 'large'
  virtualScroll?: boolean
  consistentMenuWidth?: boolean
  to?: string | HTMLElement | boolean
  menuProps?: Omit<HTMLAttributes, 'dir'> & { dir?: 'ltr' | 'rtl' }
  inputProps?: HTMLAttributes
}>(), { options: () => [], size: 'medium', virtualScroll: true, consistentMenuWidth: true, show: undefined })
const emit = defineEmits<{
  // Preserve the established NSelect callback contract at existing call sites.
  'update:value': [value: any, option: AppSelectOption | AppSelectOption[] | null]
  'update:show': [show: boolean]
  clear: []
  focus: [event: FocusEvent]
  blur: [event: FocusEvent]
}>()
const { t } = useI18n()
const settings = useSettingsStore()
const attrs = useAttrs()
const generatedId = useId()
const root = ref<HTMLElement | null>(null)
const control = ref<HTMLElement | null>(null)
const menu = ref<HTMLElement | null>(null)
const focused = ref(false)
const internalOpen = ref(false)
const menuGeneration = ref(0)
const search = ref('')
const query = ref('')
let returnFocus = true
let composing = false
let closingPointer = false
let exitSnapshot: HTMLElement | undefined
let exitMotionQuery: MediaQueryList | undefined

function clearExitSnapshot() {
  const snapshot = exitSnapshot
  exitSnapshot = undefined
  exitMotionQuery?.removeEventListener('change', onExitMotionChange)
  exitMotionQuery = undefined
  snapshot?.remove()
}
function onExitMotionChange(event: MediaQueryListEvent) {
  if (event.matches) clearExitSnapshot()
}
function captureExitSnapshot() {
  clearExitSnapshot()
  const motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
  if (!settings.animationsEnabled || motionQuery.matches) return
  const content = menu.value?.matches('.app-select__menu') ? menu.value : menu.value?.querySelector<HTMLElement>('.app-select__menu')
  const wrapper = content?.closest<HTMLElement>('[data-reka-popper-content-wrapper]')
  if (!content?.isConnected || !wrapper?.parentElement) return
  const style = getComputedStyle(content)
  if (style.visibility === 'hidden') return
  const copy = wrapper.cloneNode(true) as HTMLElement
  const copyContent = copy.querySelector<HTMLElement>('.app-select__menu')!
  // The real focus scope and aria-hidden ownership end immediately. This copy
  // only paints the remaining fade-out and must never expose duplicate IDs.
  for (const element of [copy, ...copy.querySelectorAll<HTMLElement>('*')]) {
    for (const attribute of [...element.attributes]) {
      if (attribute.name === 'id' || attribute.name === 'role' || attribute.name === 'tabindex' || attribute.name.startsWith('aria-')) element.removeAttribute(attribute.name)
    }
  }
  copy.inert = copyContent.inert = true
  copy.setAttribute('aria-hidden', 'true')
  copyContent.setAttribute('aria-hidden', 'true')
  copy.style.pointerEvents = copyContent.style.pointerEvents = 'none'
  copyContent.dataset.state = 'closed'
  copyContent.classList.add('app-select__menu--exit-snapshot')
  copyContent.style.setProperty('--app-select-exit-opacity', style.opacity)
  copyContent.style.transform = style.transform
  const finish = () => { if (exitSnapshot === copy) clearExitSnapshot() }
  copyContent.addEventListener('animationend', finish, { once: true })
  copyContent.addEventListener('animationcancel', finish, { once: true })
  exitSnapshot = copy
  exitMotionQuery = motionQuery
  motionQuery.addEventListener('change', onExitMotionChange)
  wrapper.parentElement.append(copy)
  const viewport = content.querySelector<HTMLElement>('.app-select__viewport')
  const copyViewport = copyContent.querySelector<HTMLElement>('.app-select__viewport')
  if (viewport && copyViewport) {
    copyViewport.scrollTop = viewport.scrollTop
    copyViewport.scrollLeft = viewport.scrollLeft
  }
}
onBeforeUnmount(clearExitSnapshot)
watch(() => settings.animationsEnabled, enabled => { if (!enabled) clearExitSnapshot() })

const entries = computed(() => appSelectEntries(props.options))
const byKey = computed(() => new Map(entries.value.map(entry => [entry.key, entry])))
const selected = computed(() => (Array.isArray(props.value) ? props.value : props.value == null ? [] : [props.value]))
const selectedEntries = computed(() => selected.value.map(value => byKey.value.get(appSelectKey(value))
  || { key: appSelectKey(value), value, label: String(value), group: '', disabled: false, raw: { value } }))
const knownEntries = computed(() => new Map([...selectedEntries.value.map(entry => [entry.key, entry] as const), ...byKey.value]))
const display = computed(() => selectedEntries.value.map(entry => entry.label).join(', '))
const selectedKeys = computed(() => props.multiple ? selected.value.map(appSelectKey) : selected.value.length ? appSelectKey(selected.value[0]!) : null)
const open = computed(() => !props.disabled && (props.show ?? internalOpen.value))
watch(open, (showing, wasShowing) => {
  if (showing) {
    clearExitSnapshot()
    // Reopening during a fade-out starts a fresh content lifecycle and focus scope.
    menuGeneration.value++
  } else if (wasShowing) {
    captureExitSnapshot()
    if (returnFocus && (menu.value?.contains(document.activeElement)
      || (focused.value && document.activeElement === document.body))) {
      // Restore focus now; the visual fade-out must not delay keyboard navigation.
      control.value?.focus({ preventScroll: true })
    }
  }
}, { flush: 'sync' })
const filtered = computed(() => filterAppSelectEntries(entries.value, props.filterable ? query.value : ''))
const desiredVirtual = computed(() => props.virtualScroll && entries.value.length > 100)
const virtual = ref(false)
const desiredCombo = computed(() => props.filterable || props.multiple || (props.virtualScroll && entries.value.length > 100))
const combo = ref(false)
watch([desiredCombo, desiredVirtual, open, focused], ([desired, needsVirtual, showing, hasFocus]) => {
  if (!showing && !hasFocus) {
    combo.value = desired
    virtual.value = needsVirtual
  }
}, { immediate: true })
const name = computed(() => props.ariaLabel || props.label || props.placeholder)
const controlId = computed(() => props.id || `app-select-${generatedId}`)
const portalTarget = computed(() => typeof props.to === 'string' || props.to instanceof HTMLElement ? props.to
  : root.value?.closest<HTMLElement>('[role="dialog"], .n-modal, .n-popover') || 'body')
const sections = computed(() => {
  const result: Array<{ key: string; label: string; entries: AppSelectEntry[] }> = []
  for (const entry of filtered.value) {
    let section = result.at(-1)
    if (!section || section.label !== entry.group) {
      section = { key: `section-${result.length}`, label: entry.group, entries: [] }
      result.push(section)
    }
    section.entries.push(entry)
  }
  return result
})

function setControl(instance: Element | ComponentPublicInstance | null) {
  const element = instance && '$el' in instance ? instance.$el : instance
  control.value = element instanceof HTMLElement ? element : null
}
function setMenu(instance: Element | ComponentPublicInstance | null) {
  const element = instance && '$el' in instance ? instance.$el : instance
  menu.value = element instanceof HTMLElement ? element : null
}
function onFocus(event: FocusEvent) {
  if (!focused.value) emit('focus', event)
  focused.value = true
}
function onBlur(event: FocusEvent) {
  const nextFocus = event.relatedTarget
  if (nextFocus instanceof Node && (root.value?.contains(nextFocus) || menu.value?.contains(nextFocus))) return
  void nextTick(() => {
    const active = document.activeElement
    if (root.value?.contains(active) || menu.value?.contains(active)) return
    if (open.value && active === document.body) return
    focused.value = false
    composing = false
    emit('blur', event)
  })
}
function setOpen(value: boolean) {
  if (props.disabled && value) return
  internalOpen.value = value
  query.value = ''
  emit('update:show', value)
  if (value) returnFocus = true
}
function update(keys: unknown) {
  if (props.disabled) return
  if (props.multiple && Array.isArray(keys)) {
    const values = keys.map(key => knownEntries.value.get(String(key))).filter((entry): entry is AppSelectEntry => !!entry)
    emit('update:value', values.map(entry => entry.value), values.map(entry => entry.raw))
    search.value = query.value = ''
  } else {
    const entry = byKey.value.get(String(keys))
    if (!entry || entry.disabled) return
    emit('update:value', entry.value, entry.raw)
    setOpen(false)
  }
}
function clear() {
  if (props.disabled) return
  emit('update:value', props.multiple ? [] : null, props.multiple ? [] : null)
  emit('clear')
  search.value = query.value = ''
  control.value?.focus()
}
function remove(key: string) {
  update(selectedKeys.value instanceof Array ? selectedKeys.value.filter(item => item !== key) : [])
}
function focus() { control.value?.focus() }
function blur() {
  returnFocus = false
  setOpen(false)
  control.value?.blur()
  focused.value = false
}
function closeOnEscape(event: KeyboardEvent) {
  if (!open.value) return
  event.preventDefault()
  event.stopPropagation()
  setOpen(false)
  control.value?.focus({ preventScroll: true })
}
function leaveMenu(event: KeyboardEvent) {
  if (event.key !== 'Tab') return
  // Continue the browser's normal tab order from the trigger instead of an option.
  event.stopPropagation()
  returnFocus = false
  setOpen(false)
  control.value?.focus({ preventScroll: true })
}
function keydown(event: KeyboardEvent) {
  if (composing || event.isComposing || event.defaultPrevented) return
  if (event.key === 'Escape') { closeOnEscape(event); return }
  if (event.key === 'Tab') { setOpen(false); return }
  if (event.key === 'Enter' && !open.value) { event.preventDefault(); setOpen(true) }
  else if (!props.filterable && event.key === ' ') {
    event.preventDefault()
    if (!open.value) setOpen(true)
    else control.value?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }))
  }
}
function prepareInput(event: KeyboardEvent | CompositionEvent) {
  if (event.type === 'compositionstart') composing = true
  if (event instanceof KeyboardEvent && (composing || event.isComposing)) return
  if (event instanceof KeyboardEvent && !event.isComposing && !open.value
    && (event.key === 'Enter' || (!props.filterable && event.key === ' '))) {
    // Closing options are still mounted for the fade-out. Do not let Listbox
    // consume Enter against their previous highlight before reopening.
    event.preventDefault()
    event.stopPropagation()
    setOpen(true)
    return
  }
  if (!props.filterable || props.multiple) return
  if (event instanceof KeyboardEvent && (event.key.length !== 1 || event.ctrlKey || event.metaKey || event.altKey || event.isComposing)) return
  const input = event.target as HTMLInputElement
  if (input.value === display.value) input.select()
}
function endComposition() { composing = false }
function onAnchorClick(event: MouseEvent) {
  const target = event.target as HTMLElement
  if (target.closest('button') || (target === control.value && props.filterable)) return
  event.preventDefault()
  event.stopPropagation()
  if (!props.disabled) {
    focus()
    setOpen(target === event.currentTarget || !props.filterable ? !open.value : true)
  }
}
function onTriggerPointerDown(event: PointerEvent) {
  closingPointer = open.value && event.button === 0 && !event.ctrlKey
  if (!closingPointer) return
  event.preventDefault()
  event.stopPropagation()
  setOpen(false)
}
function onTriggerPointerUp(event: PointerEvent) {
  if (!closingPointer) return
  closingPointer = false
  event.preventDefault()
  event.stopPropagation()
}
function onInput(event: Event) { query.value = (event.target as HTMLInputElement).value }
function labelForKey(key: unknown) { return knownEntries.value.get(String(key))?.label || '' }

watch([display, open], () => {
  if (!open.value) search.value = props.multiple ? '' : display.value
}, { immediate: true, deep: true })
watch(() => props.disabled, value => { if (value) setOpen(false) })
defineExpose({ focus, blur })
</script>

<template>
  <div ref="root" v-bind="attrs" class="app-select" :class="[`app-select--${size}`, { 'app-select--open': open, 'app-select--disabled': disabled }]" @focusin="onFocus" @focusout="onBlur">
    <ComboboxRoot v-if="combo" :key="String(virtual)" :model-value="selectedKeys" :open="open" :disabled="disabled" :multiple="multiple" ignore-filter open-on-click
      @update:model-value="update" @update:open="setOpen">
      <ComboboxAnchor class="app-select__control" :data-open="open || undefined" @click.capture="onAnchorClick">
        <ComboboxInput :id="controlId" :ref="setControl" v-bind="inputProps" v-model="search" class="app-select__input"
          :display-value="labelForKey" :readonly="!filterable" :aria-autocomplete="filterable ? 'list' : 'none'"
          :aria-label="name" :aria-labelledby="ariaLabelledby" :aria-busy="loading || undefined" :placeholder="multiple && selected.length ? '' : display || placeholder || t('common.pleaseSelect')"
          @input="onInput" @keydown="keydown" @keydown.capture="prepareInput" @compositionstart.capture="prepareInput" @compositionend="endComposition" />
        <span v-if="multiple && selectedEntries.length" class="app-select__tags">
          <span v-for="entry in selectedEntries" :key="entry.key" class="app-select__tag">
            <span :title="entry.label">{{ entry.label }}</span>
            <button type="button" :disabled="disabled || entry.disabled" :aria-label="t('common.remove') + ': ' + entry.label" @pointerdown.prevent @click.stop="remove(entry.key)">×</button>
          </span>
        </span>
        <button v-if="clearable && selected.length" type="button" class="app-select__clear" :disabled="disabled" :aria-label="t('common.clear') + (name ? ': ' + name : '')" @pointerdown.prevent @click.stop="clear">×</button>
        <span class="app-select__arrow" aria-hidden="true"><span v-if="loading">…</span><slot v-else name="arrow"><svg viewBox="0 0 16 16"><path d="m3.5 6 4.5 4 4.5-4" /></svg></slot></span>
      </ComboboxAnchor>
      <ComboboxPortal :to="portalTarget">
        <ComboboxContent :key="menuGeneration" :ref="setMenu" v-bind="menuProps" class="app-select__menu" :class="[`app-select__menu--${size}`, { 'app-select__menu--flexible': !consistentMenuWidth, 'app-select__menu--animated': settings.animationsEnabled }]"
          :inert="!open || undefined" :aria-hidden="!open || undefined"
          :aria-label="name" position="popper" align="start" :side-offset="4" :body-lock="false" @keydown.esc="closeOnEscape">
          <div class="app-select__viewport">
            <div v-if="!filtered.length" class="app-select__empty" role="status">{{ t(loading ? 'common.loading' : 'common.noData') }}</div>
            <ComboboxVirtualizer v-else-if="virtual" v-slot="{ option }" :options="filtered.map(entry => entry.key)" :text-content="labelForKey" :estimate-size="32">
              <ComboboxItem :value="option" :disabled="byKey.get(option)?.disabled" :text-value="labelForKey(option)" class="app-select__option"
                :aria-label="byKey.get(option)?.group ? `${byKey.get(option)?.group}: ${labelForKey(option)}` : undefined">
                <span :title="labelForKey(option)">{{ labelForKey(option) }}</span><span v-if="selected.includes(byKey.get(option)!.value)" aria-hidden="true">✓</span>
              </ComboboxItem>
            </ComboboxVirtualizer>
            <ComboboxGroup v-else v-for="section in sections" :key="section.key">
              <ComboboxLabel v-if="section.label" class="app-select__group-label">{{ section.label }}</ComboboxLabel>
              <ComboboxItem v-for="entry in section.entries" :key="entry.key" :value="entry.key" :disabled="entry.disabled" :text-value="entry.label" class="app-select__option">
                <span :title="entry.label">{{ entry.label }}</span><span v-if="selected.includes(entry.value)" aria-hidden="true">✓</span>
              </ComboboxItem>
            </ComboboxGroup>
          </div>
        </ComboboxContent>
      </ComboboxPortal>
    </ComboboxRoot>
    <SelectRoot v-else :model-value="selectedKeys" :open="open" :disabled="disabled" @update:model-value="update" @update:open="setOpen">
      <SelectTrigger :id="controlId" :ref="setControl" v-bind="inputProps" class="app-select__control" :aria-label="name" :aria-labelledby="ariaLabelledby" :aria-busy="loading || undefined"
        @pointerdown.capture="onTriggerPointerDown" @pointerup.capture="onTriggerPointerUp"
        @keydown.esc="closeOnEscape">
        <SelectValue class="app-select__value"><span v-if="!selected.length" class="app-select__placeholder">{{ placeholder || t('common.pleaseSelect') }}</span><slot v-else name="selected-value">{{ display }}</slot></SelectValue>
        <span class="app-select__arrow" aria-hidden="true"><span v-if="loading">…</span><slot v-else name="arrow"><svg viewBox="0 0 16 16"><path d="m3.5 6 4.5 4 4.5-4" /></svg></slot></span>
      </SelectTrigger>
      <button v-if="clearable && selected.length" type="button" class="app-select__clear app-select__clear--single" :disabled="disabled" :aria-label="t('common.clear') + (name ? ': ' + name : '')" @pointerdown.prevent @click.stop="clear">×</button>
      <SelectPortal :to="portalTarget">
        <SelectContent :key="menuGeneration" :ref="setMenu" v-bind="menuProps" class="app-select__menu" :class="[`app-select__menu--${size}`, { 'app-select__menu--flexible': !consistentMenuWidth, 'app-select__menu--animated': settings.animationsEnabled }]" @focusin="onFocus" @focusout="onBlur"
          :inert="!open || undefined" :aria-hidden="!open || undefined"
          :aria-label="name" position="popper" align="start" :side-offset="4" :body-lock="false" :disable-outside-pointer-events="false" @keydown.esc="closeOnEscape" @keydown.capture="leaveMenu"
          @close-auto-focus.prevent>
          <SelectViewport class="app-select__viewport">
            <div v-if="!filtered.length" class="app-select__empty" role="status">{{ t(loading ? 'common.loading' : 'common.noData') }}</div>
            <SelectGroup v-for="section in sections" :key="section.key">
              <SelectLabel v-if="section.label" class="app-select__group-label">{{ section.label }}</SelectLabel>
              <SelectItem v-for="entry in section.entries" :key="entry.key" :value="entry.key" :disabled="entry.disabled" :text-value="entry.label" class="app-select__option">
                <SelectItemText :title="entry.label">{{ entry.label }}</SelectItemText><span v-if="selected.includes(entry.value)" aria-hidden="true">✓</span>
              </SelectItem>
            </SelectGroup>
          </SelectViewport>
        </SelectContent>
      </SelectPortal>
    </SelectRoot>
  </div>
</template>

<style>
.app-select { --app-select-height: 34px; --app-select-font: 14px; position: relative; width: 100%; min-width: 0; font: inherit; }
.app-select--tiny { --app-select-height: 22px; --app-select-font: 12px; }
.app-select--small { --app-select-height: 28px; }
.app-select--large { --app-select-height: 40px; --app-select-font: 15px; }
.app-select__control {
  display: flex; align-items: center; gap: 6px; min-width: 0; width: 100%; min-height: var(--app-select-height);
  padding: 0 10px 0 12px; border: 1px solid var(--app-select-border, var(--outline)); border-radius: 11px;
  background: var(--app-select-color, var(--surface-2)); color: var(--on-surface-muted); font: inherit; font-size: var(--app-select-font); text-align: left; cursor: pointer;
}
.dark-theme .app-select__control { color: var(--on-surface); }
.app-select:not(.app-select--disabled) .app-select__control:hover { border-color: var(--app-select-border-hover, var(--primary)); }
.app-select__control:focus-visible, .app-select__control:focus-within, .app-select--open .app-select__control {
  border-color: var(--app-select-border-focus, var(--primary)); outline: 2px solid transparent; outline-offset: 2px; box-shadow: var(--app-select-focus-shadow, 0 0 0 3px color-mix(in srgb, var(--primary-glow) 52%, transparent));
}
.dark-theme .app-select { --app-select-focus-shadow: 0 0 0 3px color-mix(in srgb, var(--primary-glow) 62%, transparent); }
.app-select--disabled { opacity: .55; }
.app-select--disabled .app-select__control, .app-select--disabled .app-select__input { cursor: not-allowed; }
.app-select__value { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; min-width: 0; flex: 1; }
.app-select__placeholder { color: var(--on-surface-muted); }
.app-select__input { order: 1; width: 100%; min-width: 24px; flex: 1; height: calc(var(--app-select-height) - 2px); padding: 0; border: 0; outline: 0; background: transparent; color: inherit; font: inherit; cursor: inherit; }
.app-select__input::placeholder { color: var(--on-surface-muted); }
.app-select__arrow { order: 3; display: inline-flex; flex-shrink: 0; align-items: center; justify-content: center; width: 16px; color: var(--on-surface-muted); opacity: .65; font-size: 16px; line-height: 1; pointer-events: none; margin-left: auto; }
.app-select__arrow svg { width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-width: 1; stroke-linecap: round; stroke-linejoin: round; }
.app-select__clear { order: 2; flex-shrink: 0; width: 20px; height: 22px; padding: 0; border: 0; border-radius: 4px; background: transparent; color: var(--on-surface-muted); font: inherit; cursor: pointer; }
.app-select__clear:hover { color: var(--on-surface); background: var(--surface-3); }
.app-select__clear--single { position: absolute; inset-inline-end: 29px; top: 50%; transform: translateY(-50%); background: var(--app-select-color, var(--surface-2)); }
.app-select__menu { z-index: 3100; min-width: var(--reka-select-trigger-width, var(--reka-combobox-trigger-width)); width: var(--reka-select-trigger-width, var(--reka-combobox-trigger-width)); max-width: calc(100vw - 16px); padding: 4px; border: 1px solid var(--outline); border-radius: 11px; background: var(--surface-1); color: var(--on-surface); box-shadow: var(--shadow-panel); font: inherit; font-size: 14px; }
.app-select__menu--tiny { font-size: 12px; }
.app-select__menu--flexible { width: max-content; }
.app-select__menu { --app-select-enter-y: -3px; }
.app-select__menu[data-side='top'] { --app-select-enter-y: 3px; }
.app-select__menu--animated[data-state='open'] { animation: app-select-enter 140ms cubic-bezier(.2, .8, .2, 1); }
.app-select__menu--animated.app-select__menu--exit-snapshot { animation: app-select-leave 80ms ease-out; }
.app-select__menu[data-state='closed'] { pointer-events: none; }
@keyframes app-select-enter {
  from { opacity: 0; transform: translateY(var(--app-select-enter-y)); }
  to { opacity: 1; transform: translateY(0); }
}
@keyframes app-select-leave {
  from { opacity: var(--app-select-exit-opacity, 1); }
  to { opacity: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .app-select__menu.app-select__menu--animated { animation: none; }
}
.app-select__viewport { min-height: 0; max-height: min(300px, var(--reka-select-content-available-height, var(--reka-combobox-content-available-height, 300px))); overflow: auto; width: 100%; }
.app-select__option { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 32px; padding: 6px 9px; border-radius: 7px; outline: none; cursor: pointer; overflow-wrap: anywhere; }
.app-select__option > span:first-child { flex: 1; min-width: 0; overflow: hidden; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.app-select__option > span[aria-hidden] { flex: 0 0 auto; }
.app-select__option[data-state='checked'] { color: var(--primary-strong); background: var(--primary-softer); }
.app-select__option[data-highlighted], .app-select__option:focus { background: var(--primary-soft); }
.app-select__option[data-disabled] { opacity: .45; cursor: not-allowed; }
.app-select__option[data-index] { height: 32px; width: 100%; }
.app-select__option[data-index] > span:first-child { display: block; white-space: nowrap; text-overflow: ellipsis; }
.app-select__group-label { padding: 7px 9px 4px; color: var(--on-surface-muted); font-size: 12px; }
.app-select__empty { padding: 16px 9px; text-align: center; color: var(--on-surface-muted); }
.app-select__tags { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; min-width: 0; padding-block: 3px; }
.app-select__tag { display: inline-flex; align-items: center; gap: 4px; max-width: 100%; padding: 1px 4px 1px 6px; border-radius: 5px; background: var(--surface-3); font-size: 12px; }
.app-select__tag > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.app-select__tag button { background: none; border: 0; padding: 0 2px; color: inherit; font: inherit; cursor: pointer; }
@media (forced-colors: active) {
  .app-select__control, .app-select__menu { background: Canvas; color: CanvasText; border-color: ButtonText; }
  .app-select__control:focus-visible, .app-select__control:focus-within, .app-select--open .app-select__control { outline-color: Highlight; }
  .app-select__option[data-highlighted], .app-select__option:focus { background: Highlight; color: HighlightText; }
  .app-select--disabled, .app-select__option[data-disabled] { opacity: 1; color: GrayText; }
}
</style>
