import { ref } from 'vue'
import type { GlobalThemeOverrides } from 'naive-ui'

export type ThemeMode = 'system' | 'dark' | 'light'
export type ThemeAccent = 'blue' | 'pink' | 'sky' | 'teal'

type ThemePalette = {
  primary: string
  primaryStrong: string
  primarySoft: string
  primarySofter: string
  primaryBorder: string
  primaryGlow: string
  primaryHover: string
  primaryPressed: string
  preview: [string, string]
}

type SurfacePalette = {
  surface: string
  surface1: string
  surface2: string
  surface3: string
  onSurfaceHeading: string
  onSurface: string
  onSurfaceMuted: string
  outline: string
  outlineStrong: string
  success: string
  warning: string
  danger: string
  shadowPanel: string
  shadowSoft: string
}

export type ResolvedThemeTokens = SurfacePalette & ThemePalette & {
  isDark: boolean
}

type ViewTransitionLike = {
  ready: Promise<void>
}

type DocumentWithViewTransition = Document & {
  startViewTransition?: (callback: () => void | Promise<void>) => ViewTransitionLike
}

const RIPPLE_TRANSITION_DURATION = 700
const RIPPLE_TRANSITION_EASING = 'cubic-bezier(0.2, 0, 0, 1)'

const DARK_SURFACE: SurfacePalette = {
  surface: '#111317',
  surface1: '#191c21',
  surface2: '#23272e',
  surface3: '#303640',
  onSurfaceHeading: '#e5e7eb',
  onSurface: '#c9cdd4',
  onSurfaceMuted: '#969da8',
  outline: 'rgba(229, 231, 235, 0.08)',
  outlineStrong: 'rgba(229, 231, 235, 0.14)',
  success: '#7fb59a',
  warning: '#d0ab70',
  danger: '#d7808f',
  shadowPanel: '0 6px 18px rgba(0, 0, 0, 0.12)',
  shadowSoft: '0 14px 40px rgba(0, 0, 0, 0.32)',
}

const LIGHT_SURFACE: SurfacePalette = {
  surface: '#f4f6fa',
  surface1: '#fbfcff',
  surface2: '#eef2f7',
  surface3: '#dfe5ee',
  onSurfaceHeading: '#172033',
  onSurface: '#172033',
  onSurfaceMuted: '#596579',
  outline: 'rgba(31, 42, 68, 0.13)',
  outlineStrong: 'rgba(31, 42, 68, 0.22)',
  success: '#2e7d58',
  warning: '#9a6b00',
  danger: '#b4233a',
  shadowPanel: '0 18px 46px rgba(33, 43, 67, 0.07)',
  shadowSoft: '0 20px 70px rgba(33, 43, 67, 0.11)',
}

const THEME_PALETTES: Record<ThemeAccent, { light: ThemePalette; dark: ThemePalette }> = {
  blue: {
    dark: {
      primary: '#7c9ed9',
      primaryStrong: '#b7ccee',
      primarySoft: 'rgba(124, 158, 217, 0.15)',
      primarySofter: 'rgba(124, 158, 217, 0.075)',
      primaryBorder: 'rgba(124, 158, 217, 0.30)',
      primaryGlow: 'rgba(124, 158, 217, 0.08)',
      primaryHover: '#90aee4',
      primaryPressed: '#6a8bc3',
      preview: ['#7c9ed9', '#b7ccee'],
    },
    light: {
      primary: '#3f70d4',
      primaryStrong: '#2454ad',
      primarySoft: 'rgba(63, 112, 212, 0.13)',
      primarySofter: 'rgba(63, 112, 212, 0.07)',
      primaryBorder: 'rgba(63, 112, 212, 0.24)',
      primaryGlow: 'rgba(63, 112, 212, 0.14)',
      primaryHover: '#2f61c2',
      primaryPressed: '#244f9f',
      preview: ['#3f70d4', '#83a7ea'],
    },
  },
  pink: {
    dark: {
      primary: '#d084ad',
      primaryStrong: '#edbdd2',
      primarySoft: 'rgba(208, 132, 173, 0.18)',
      primarySofter: 'rgba(208, 132, 173, 0.085)',
      primaryBorder: 'rgba(208, 132, 173, 0.36)',
      primaryGlow: 'rgba(208, 132, 173, 0.14)',
      primaryHover: '#dd9cbd',
      primaryPressed: '#b86f95',
      preview: ['#d084ad', '#edbdd2'],
    },
    light: {
      primary: '#d56f9f',
      primaryStrong: '#b44e7f',
      primarySoft: 'rgba(213, 111, 159, 0.18)',
      primarySofter: 'rgba(213, 111, 159, 0.08)',
      primaryBorder: 'rgba(213, 111, 159, 0.34)',
      primaryGlow: 'rgba(213, 111, 159, 0.20)',
      primaryHover: '#b44e7f',
      primaryPressed: '#b44e7f',
      preview: ['#d56f9f', '#f2b9d3'],
    },
  },
  sky: {
    dark: {
      primary: '#70b4c8',
      primaryStrong: '#b4d9e2',
      primarySoft: 'rgba(112, 180, 200, 0.18)',
      primarySofter: 'rgba(112, 180, 200, 0.085)',
      primaryBorder: 'rgba(112, 180, 200, 0.36)',
      primaryGlow: 'rgba(112, 180, 200, 0.14)',
      primaryHover: '#89c4d4',
      primaryPressed: '#5997aa',
      preview: ['#70b4c8', '#b4d9e2'],
    },
    light: {
      primary: '#4d8fd8',
      primaryStrong: '#2f74c0',
      primarySoft: 'rgba(77, 143, 216, 0.18)',
      primarySofter: 'rgba(77, 143, 216, 0.08)',
      primaryBorder: 'rgba(77, 143, 216, 0.34)',
      primaryGlow: 'rgba(77, 143, 216, 0.20)',
      primaryHover: '#2f74c0',
      primaryPressed: '#2f74c0',
      preview: ['#4d8fd8', '#9bcbf5'],
    },
  },
  teal: {
    dark: {
      primary: '#70b69d',
      primaryStrong: '#b0d9c9',
      primarySoft: 'rgba(112, 182, 157, 0.18)',
      primarySofter: 'rgba(112, 182, 157, 0.085)',
      primaryBorder: 'rgba(112, 182, 157, 0.36)',
      primaryGlow: 'rgba(112, 182, 157, 0.14)',
      primaryHover: '#89c7b0',
      primaryPressed: '#579982',
      preview: ['#70b69d', '#b0d9c9'],
    },
    light: {
      primary: '#238c7b',
      primaryStrong: '#156f61',
      primarySoft: 'rgba(35, 140, 123, 0.18)',
      primarySofter: 'rgba(35, 140, 123, 0.08)',
      primaryBorder: 'rgba(35, 140, 123, 0.34)',
      primaryGlow: 'rgba(35, 140, 123, 0.20)',
      primaryHover: '#156f61',
      primaryPressed: '#156f61',
      preview: ['#238c7b', '#84d6ca'],
    },
  },
}

export const DEFAULT_THEME_MODE: ThemeMode = 'system'
export const DEFAULT_THEME_ACCENT: ThemeAccent = 'blue'
export const THEME_ACCENTS: ThemeAccent[] = ['blue', 'pink', 'sky', 'teal']

let currentMode: ThemeMode = DEFAULT_THEME_MODE
let currentAccent: ThemeAccent = DEFAULT_THEME_ACCENT

/**
 * Reactive mirror of the applied dark state. matchMedia is not reactive, so computeds that
 * derive Naive UI theme data read this instead — otherwise an OS scheme flip while following
 * system mode would swap the CSS variables but leave component theming stale.
 */
export const themeIsDark = ref(resolvedIsDark(currentMode))

function isThemeMode(value: string | null | undefined): value is ThemeMode {
  return value === 'system' || value === 'dark' || value === 'light'
}

function isThemeAccent(value: string | null | undefined): value is ThemeAccent {
  return value === 'blue' || value === 'pink' || value === 'sky' || value === 'teal'
}

export function normalizeThemeMode(value: string | null | undefined, fallback: ThemeMode = DEFAULT_THEME_MODE): ThemeMode {
  return isThemeMode(value) ? value : fallback
}

export function normalizeThemeAccent(
  value: string | null | undefined,
  fallback: ThemeAccent = DEFAULT_THEME_ACCENT,
): ThemeAccent {
  return isThemeAccent(value) ? value : fallback
}

export function resolvedIsDark(mode: ThemeMode = currentMode): boolean {
  return mode === 'dark' || (mode === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)
}

export function getResolvedThemeTokens(
  mode: ThemeMode = currentMode,
  accent: ThemeAccent = currentAccent,
): ResolvedThemeTokens {
  const dark = resolvedIsDark(mode)
  const surface = dark ? DARK_SURFACE : LIGHT_SURFACE
  const palette = THEME_PALETTES[accent][dark ? 'dark' : 'light']
  return {
    ...surface,
    ...palette,
    isDark: dark,
  }
}

export function getThemeAccentPreview(accent: ThemeAccent, dark: boolean) {
  return THEME_PALETTES[accent][dark ? 'dark' : 'light'].preview
}

function applyThemeClass(mode: ThemeMode) {
  const dark = resolvedIsDark(mode)
  document.documentElement.classList.toggle('dark-theme', dark)
  document.documentElement.classList.toggle('light-theme', !dark)
}

function applyThemeTokens(mode: ThemeMode, accent: ThemeAccent) {
  const tokens = getResolvedThemeTokens(mode, accent)
  const style = document.documentElement.style
  style.setProperty('--surface', tokens.surface)
  style.setProperty('--surface-1', tokens.surface1)
  style.setProperty('--surface-2', tokens.surface2)
  style.setProperty('--surface-3', tokens.surface3)
  style.setProperty('--on-surface-heading', tokens.onSurfaceHeading)
  style.setProperty('--on-surface', tokens.onSurface)
  style.setProperty('--on-surface-muted', tokens.onSurfaceMuted)
  style.setProperty('--outline', tokens.outline)
  style.setProperty('--outline-strong', tokens.outlineStrong)
  style.setProperty('--primary', tokens.primary)
  style.setProperty('--primary-strong', tokens.primaryStrong)
  style.setProperty('--primary-soft', tokens.primarySoft)
  style.setProperty('--primary-softer', tokens.primarySofter)
  style.setProperty('--primary-border', tokens.primaryBorder)
  style.setProperty('--primary-glow', tokens.primaryGlow)
  style.setProperty('--success', tokens.success)
  style.setProperty('--warning', tokens.warning)
  style.setProperty('--danger', tokens.danger)
  style.setProperty('--shadow-panel', tokens.shadowPanel)
  style.setProperty('--shadow-soft', tokens.shadowSoft)
}

export function getThemeOverrides(
  mode: ThemeMode = currentMode,
  accent: ThemeAccent = currentAccent,
): GlobalThemeOverrides {
  const tokens = getResolvedThemeTokens(mode, accent)
  // Keep component surfaces and solid primary-button labels on the same dark base.
  const darkBase = tokens.isDark ? {
    baseColor: tokens.surface,
    textColorBase: tokens.onSurface,
  } : {}
  return {
    common: {
      ...darkBase,
      bodyColor: tokens.surface,
      cardColor: tokens.surface1,
      modalColor: tokens.surface1,
      popoverColor: tokens.surface1,
      tableColor: tokens.surface1,
      inputColor: tokens.surface2,
      actionColor: tokens.surface2,
      hoverColor: tokens.surface2,
      dividerColor: tokens.outline,
      borderColor: tokens.outline,
      textColor1: tokens.onSurfaceHeading,
      textColor2: tokens.isDark ? tokens.onSurface : tokens.onSurfaceMuted,
      textColor3: tokens.onSurfaceMuted,
      primaryColor: tokens.primary,
      primaryColorHover: tokens.primaryHover,
      primaryColorPressed: tokens.primaryPressed,
      primaryColorSuppl: tokens.primary,
      successColor: tokens.success,
      warningColor: tokens.warning,
      errorColor: tokens.danger,
      placeholderColor: tokens.onSurfaceMuted,
      tableHeaderColor: tokens.surface2,
    },
    Card: {
      paddingMedium: '20px',
      borderRadius: '16px',
      titleTextColor: tokens.onSurface,
      color: tokens.surface1,
      borderColor: tokens.outline,
    },
    Button: {
      borderRadius: '11px',
      textColor: tokens.onSurface,
      color: tokens.surface2,
      borderColor: tokens.outline,
      colorHover: tokens.surface3,
      textColorHover: tokens.onSurface,
      ...(tokens.isDark ? {
        textColor: tokens.onSurfaceHeading,
        textColorHover: tokens.onSurfaceHeading,
        textColorPressed: tokens.onSurfaceHeading,
        textColorFocus: tokens.onSurfaceHeading,
        colorSecondary: tokens.surface2,
        colorSecondaryHover: tokens.surface3,
        colorSecondaryPressed: tokens.surface2,
        colorDisabled: tokens.surface2,
        colorDisabledPrimary: tokens.surface2,
        textColorDisabled: tokens.onSurfaceMuted,
        textColorDisabledPrimary: tokens.onSurfaceMuted,
        borderDisabledPrimary: `1px solid ${tokens.outline}`,
        opacityDisabled: '0.55',
      } : {}),
    },
    Input: {
      borderRadius: '11px',
      color: tokens.surface2,
      textColor: tokens.onSurface,
      border: `1px solid ${tokens.outline}`,
      borderHover: `1px solid ${tokens.primary}`,
      borderFocus: `1px solid ${tokens.primary}`,
    },
    Select: {
      borderRadius: '11px',
      menuColor: tokens.surface1,
    },
    Tag: {
      borderRadius: '8px',
    },
    Progress: {
      railColor: tokens.surface3,
    },
    Menu: {
      itemColorActive: tokens.primarySoft,
      itemTextColorActive: tokens.primaryStrong,
      itemTextColor: tokens.onSurfaceMuted,
      itemColorHover: tokens.surface2,
      itemTextColorHover: tokens.onSurface,
      borderRadius: '10px',
    },
    Steps: {
      stepHeaderFontSizeSmall: '14px',
      stepIndicatorTextColorFinished: tokens.primary,
      stepHeaderTextColorFinished: tokens.onSurface,
      stepIndicatorBorderColorFinished: tokens.primary,
      connectorColor: tokens.outline,
    },
    Collapse: {
      titleTextColor: tokens.onSurface,
      titleTextColorDisabled: tokens.onSurfaceMuted,
      dividerColor: tokens.outline,
      borderRadius: '12px',
    },
    DataTable: {
      tdColor: tokens.surface1,
      thColor: tokens.surface2,
      borderColor: tokens.outline,
    },
    Slider: {
      railColor: tokens.surface3,
    },
    Switch: {
      railColor: tokens.surface3,
      railColorActive: tokens.primary,
    },
    InputNumber: {
      color: tokens.surface2,
      border: `1px solid ${tokens.outline}`,
      borderHover: `1px solid ${tokens.primary}`,
      borderFocus: `1px solid ${tokens.primary}`,
    },
  }
}

export function applyTheme(mode: ThemeMode, accent: ThemeAccent = currentAccent) {
  currentMode = normalizeThemeMode(mode)
  currentAccent = normalizeThemeAccent(accent)
  themeIsDark.value = resolvedIsDark(currentMode)
  applyThemeClass(currentMode)
  applyThemeTokens(currentMode, currentAccent)
}

export function initTheme(
  mode: ThemeMode = DEFAULT_THEME_MODE,
  accent: ThemeAccent = DEFAULT_THEME_ACCENT,
) {
  applyTheme(mode, accent)
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (currentMode === 'system') applyTheme(currentMode, currentAccent)
  })
}

function isLinuxWebKit() {
  return /Linux/i.test(navigator.platform)
    && /AppleWebKit/i.test(navigator.userAgent)
    && !/Chrom(?:e|ium)|Edg|Android/i.test(navigator.userAgent)
}

export async function runRippleViewTransition(
  update: () => void | Promise<void>,
  origin?: { x: number; y: number },
) {
  const doc = document as DocumentWithViewTransition
  // WebKitGTK can segfault in startViewTransition when accelerated compositing is unavailable.
  // Skip the native API entirely: https://bugs.webkit.org/show_bug.cgi?id=324730
  if (!origin || !doc.startViewTransition || isLinuxWebKit()) {
    await update()
    return
  }

  const { x, y } = origin
  const maxRadius = Math.hypot(
    Math.max(x, window.innerWidth - x),
    Math.max(y, window.innerHeight - y),
  )

  const transition = doc.startViewTransition(async () => {
    await update()
  })

  try {
    await transition.ready
    document.documentElement.animate(
      {
        clipPath: [
          `circle(0px at ${x}px ${y}px)`,
          `circle(${maxRadius}px at ${x}px ${y}px)`,
        ],
        opacity: [0.7, 1],
      },
      {
        duration: RIPPLE_TRANSITION_DURATION,
        easing: RIPPLE_TRANSITION_EASING,
        pseudoElement: '::view-transition-new(root)',
      },
    )
  } catch {
    // ignore interrupted transitions
  }
}
