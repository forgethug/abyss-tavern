/**
 * 探险日志名字颜色：纯客户端 localStorage 存储，每人本地独立设置。
 * key: 冒险者名字 → color hex string
 */
import { ref } from 'vue'

const STORAGE_KEY = 's1_name_colors'
const AUTO_HIGHLIGHT_ENABLED_KEY = 's1_name_colors_auto_enabled'

function load(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
  } catch {
    return {}
  }
}

function save(map: Record<string, string>) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(map))
}

function loadAutoHighlightEnabled(): boolean {
  try {
    return localStorage.getItem(AUTO_HIGHLIGHT_ENABLED_KEY) === '1'
  } catch {
    return false
  }
}

function saveAutoHighlightEnabled(enabled: boolean) {
  localStorage.setItem(AUTO_HIGHLIGHT_ENABLED_KEY, enabled ? '1' : '0')
}

const colorMap = ref<Record<string, string>>(load())
const autoHighlightEnabled = ref<boolean>(loadAutoHighlightEnabled())

export function useNameColors() {
  function setColor(name: string, color: string | null) {
    const next = { ...colorMap.value }
    if (color) {
      next[name] = color
    } else {
      delete next[name]
    }
    colorMap.value = next
    save(next)
  }

  function getColor(name: string): string | undefined {
    return colorMap.value[name]
  }

  function setAutoHighlightEnabled(enabled: boolean) {
    autoHighlightEnabled.value = enabled
    saveAutoHighlightEnabled(enabled)
  }

  return {
    colorMap,
    setColor,
    getColor,
    autoHighlightEnabled,
    setAutoHighlightEnabled,
  }
}
