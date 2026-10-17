import { ref, watch, type Ref } from 'vue'


export function useStorage<T>(key: string, defaultValue: T): Ref<T> {
  const stored = localStorage.getItem(key)
  let initial = defaultValue
  if (stored !== null) {
    try {
      initial = JSON.parse(stored) as T
    } catch {
      initial = stored as T
    }
  }
  const value = ref(initial) as Ref<T>
  watch(
    value,
    (next) => localStorage.setItem(key, JSON.stringify(next)),
    { deep: true },
  )
  return value
}
