import { ref } from 'vue'


type Handler = (...args: any[]) => void
const lastEventAt = ref(0)

export function useSocket() {
  function debouncedCallback<T extends unknown[]>(
    callback: (...args: T) => void,
    delay = 1000,
  ) {
    let timer: ReturnType<typeof setTimeout> | null = null
    return (...args: T) => {
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => {
        callback(...args)
        timer = null
      }, delay)
    }
  }

  return {
    connect: () => undefined,
    disconnect: () => undefined,
    registerPage: (_pageName: string, _autoConnect = true) => undefined,
    unregisterPage: () => undefined,
    on: (_event: string, _callback: Handler) => undefined,
    off: (_event: string, _callback?: Handler) => undefined,
    emit: (_event: string, ..._args: any[]) => undefined,
    isConnected: () => false,
    isSocketHealthy: () => false,
    lastEventAt,
    debouncedCallback,
  }
}
