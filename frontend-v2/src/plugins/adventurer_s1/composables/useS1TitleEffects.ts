import { ref } from 'vue'

const STORAGE_KEY = 's1_title_animations_enabled'
const DISABLED_CLASS = 's1-title-effects-disabled'

function loadEnabled(): boolean {
  if (typeof window === 'undefined') return true
  try {
    return window.localStorage.getItem(STORAGE_KEY) !== '0'
  } catch {
    return true
  }
}

const titleEffectsEnabled = ref(loadEnabled())

function applyToDocument(enabled: boolean) {
  if (typeof document === 'undefined') return
  document.documentElement.classList.toggle(DISABLED_CLASS, !enabled)
}

function setTitleEffectsEnabled(enabled: boolean) {
  titleEffectsEnabled.value = enabled
  applyToDocument(enabled)
  try {
    window.localStorage.setItem(STORAGE_KEY, enabled ? '1' : '0')
  } catch {
    // 浏览器禁用本地存储时，本次页面内设置仍然生效。
  }
}

applyToDocument(titleEffectsEnabled.value)

if (typeof window !== 'undefined') {
  window.addEventListener('storage', (event) => {
    if (event.key !== STORAGE_KEY) return
    const enabled = event.newValue !== '0'
    titleEffectsEnabled.value = enabled
    applyToDocument(enabled)
  })
}

export function useS1TitleEffects() {
  return {
    titleEffectsEnabled,
    setTitleEffectsEnabled,
  }
}
