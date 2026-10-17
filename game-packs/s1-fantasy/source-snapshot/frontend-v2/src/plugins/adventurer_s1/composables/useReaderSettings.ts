import { computed } from 'vue'
import { useStorage } from '@/utils/storage'

export type ReaderTheme = 'default' | 'paper' | 'dark' | 'eye'
export type ReaderFontFamily = 'system' | 'sans' | 'serif' | 'kai' | 'mono'
export type ReaderFontWeight = 400 | 500 | 600
export type ReaderWidth = 'narrow' | 'standard' | 'wide'
export type ReaderBackgroundFit = 'cover' | 'contain'

export interface ReaderPrefs {
  fontSize: number
  theme: ReaderTheme
  lineHeight: number
  fontFamily: ReaderFontFamily
  fontWeight: ReaderFontWeight
  letterSpacing: number
  paragraphSpacing: number
  readerWidth: ReaderWidth
  backgroundImageOpacity: number
  backgroundPanelOpacity: number
  backgroundBlur: number
  backgroundFit: ReaderBackgroundFit
}

const STORAGE_KEY = 'adventurer_s1_reader_prefs'

const DEFAULTS: ReaderPrefs = {
  fontSize: 16,
  theme: 'default',
  lineHeight: 1.85,
  fontFamily: 'system',
  fontWeight: 400,
  letterSpacing: 0,
  paragraphSpacing: 16,
  readerWidth: 'standard',
  backgroundImageOpacity: 0.55,
  backgroundPanelOpacity: 0.88,
  backgroundBlur: 4,
  backgroundFit: 'cover',
}

const THEME_LABELS: Record<ReaderTheme, string> = {
  default: '跟随主题',
  paper: '羊皮纸',
  dark: '夜间',
  eye: '护眼',
}

export const READER_FONT_OPTIONS: [ReaderFontFamily, string][] = [
  ['system', '系统默认'],
  ['sans', '系统黑体'],
  ['serif', '系统宋体'],
  ['kai', '系统楷体'],
  ['mono', '等宽字体'],
]

export const READER_FONT_STACKS: Record<ReaderFontFamily, string> = {
  system: 'system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif',
  sans: '"Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", system-ui, sans-serif',
  serif: 'ui-serif, "Songti SC", STSong, SimSun, serif',
  kai: 'KaiTi, STKaiti, "Kaiti SC", ui-serif, serif',
  mono: 'ui-monospace, "Cascadia Mono", Consolas, monospace',
}

export const READER_WIDTHS: Record<ReaderWidth, number> = {
  narrow: 600,
  standard: 720,
  wide: 900,
}

export function useReaderSettings() {
  const prefs = useStorage<ReaderPrefs>(STORAGE_KEY, { ...DEFAULTS })
  prefs.value = { ...DEFAULTS, ...prefs.value }

  const fontSize = computed({
    get: () => prefs.value.fontSize ?? DEFAULTS.fontSize,
    set: (v: number) => {
      prefs.value = { ...prefs.value, fontSize: v }
    },
  })

  const theme = computed({
    get: () => prefs.value.theme ?? DEFAULTS.theme,
    set: (v: ReaderTheme) => {
      prefs.value = { ...prefs.value, theme: v }
    },
  })

  const lineHeight = computed({
    get: () => prefs.value.lineHeight ?? DEFAULTS.lineHeight,
    set: (v: number) => {
      prefs.value = { ...prefs.value, lineHeight: v }
    },
  })

  const fontSizeLabel = computed(() => {
    const s = fontSize.value
    if (s >= 18) return 'A+'
    if (s <= 14) return 'A-'
    return 'A'
  })

  function changeFontSize(delta: number) {
    fontSize.value = Math.max(12, Math.min(24, fontSize.value + delta))
  }

  function cycleTheme() {
    const order: ReaderTheme[] = ['default', 'paper', 'dark', 'eye']
    const idx = order.indexOf(theme.value)
    theme.value = order[(idx + 1) % order.length]
  }

  function setTheme(next: ReaderTheme) {
    theme.value = next
  }

  function updatePrefs(patch: Partial<ReaderPrefs>) {
    prefs.value = { ...DEFAULTS, ...prefs.value, ...patch }
  }

  function resetPrefs() {
    prefs.value = { ...DEFAULTS }
  }

  function themeLabel(t: ReaderTheme = theme.value) {
    return THEME_LABELS[t]
  }

  return {
    prefs,
    fontSize,
    theme,
    lineHeight,
    fontSizeLabel,
    changeFontSize,
    cycleTheme,
    setTheme,
    updatePrefs,
    resetPrefs,
    themeLabel,
    themeOptions: Object.entries(THEME_LABELS) as [ReaderTheme, string][],
    fontOptions: READER_FONT_OPTIONS,
  }
}
