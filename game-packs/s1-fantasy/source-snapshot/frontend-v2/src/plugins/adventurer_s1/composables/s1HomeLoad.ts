// Only the homepage's read requests use this recovery policy.
const INIT_MAX_ATTEMPTS = 3
const FALLBACK_CONCURRENCY = 2
const RETRY_BASE_DELAY_MS = 1000
const RETRY_JITTER_MS = 500
const MAX_AUTOMATIC_DELAY_MS = 30_000

interface HomeRead {
  key: string
  load: () => Promise<any>
}

interface HomeLoadOptions {
  loadInit: (force: boolean) => Promise<any>
  force: boolean
  fallback: HomeRead[]
  signal: AbortSignal
  isCurrent: () => boolean
  onRetry: () => void
}

export interface HomeLoadResult {
  kind: 'ready' | 'fallback' | 'busy' | 'unavailable' | 'cancelled'
  loaded: string[]
  response?: any
  error?: any
}

function isBusy(error: any): boolean {
  return Number(error?.response?.status ?? error?.code) === 503
}

function isTransportUnavailable(error: any): boolean {
  const status = Number(error?.response?.status ?? error?.code)
  return status === 408 || status === 504
    || ['ECONNABORTED', 'ETIMEDOUT', 'ERR_NETWORK'].includes(error?.code)
}

function retryDelay(error: any, attempt: number): number {
  const headers = error?.response?.headers
  const raw = headers?.get?.('retry-after') ?? headers?.['retry-after']
  const seconds = raw == null || String(raw).trim() === '' ? NaN : Number(raw)
  const hint = Number.isFinite(seconds)
    ? seconds * 1000
    : Date.parse(String(raw)) - Date.now()
  const delay = Number.isFinite(hint)
    ? Math.max(RETRY_BASE_DELAY_MS, hint)
    : RETRY_BASE_DELAY_MS * (attempt + 1)
  return delay + Math.floor(Math.random() * RETRY_JITTER_MS)
}

function waitForRetry(delay: number, signal: AbortSignal): Promise<boolean> {
  if (signal.aborted) return Promise.resolve(false)
  return new Promise(resolve => {
    const finish = (ready: boolean) => {
      clearTimeout(timer)
      signal.removeEventListener('abort', cancel)
      resolve(ready)
    }
    const cancel = () => finish(false)
    const timer = setTimeout(() => finish(true), delay)
    signal.addEventListener('abort', cancel, { once: true })
  })
}

export async function loadS1Home(options: HomeLoadOptions): Promise<HomeLoadResult> {
  const current = () => !options.signal.aborted && options.isCurrent()
  const cancelled = (): HomeLoadResult => ({ kind: 'cancelled', loaded: [] })
  let failure: any
  for (let attempt = 0; attempt < INIT_MAX_ATTEMPTS; attempt += 1) {
    if (!current()) return cancelled()
    try {
      const response = await options.loadInit(options.force || attempt > 0)
      if (!current() || response?.stale_session) return cancelled()
      if (response?.code === 200 && (response.data || response.cached === true)) {
        return { kind: 'ready', loaded: [], response }
      }
      failure = response
    } catch (error) {
      failure = error
    }
    if (!current()) return cancelled()
    // A client timeout does not cancel server work. Do not overlap it with
    // another init or the nine independent fallback reads.
    if (isTransportUnavailable(failure)) {
      return { kind: 'unavailable', loaded: [], error: failure }
    }
    if (!isBusy(failure)) break
    const delay = retryDelay(failure, attempt)
    if (attempt + 1 === INIT_MAX_ATTEMPTS || delay > MAX_AUTOMATIC_DELAY_MS) {
      return { kind: 'busy', loaded: [], error: failure }
    }
    options.onRetry()
    if (!await waitForRetry(delay, options.signal)) return cancelled()
  }

  // Keep the independent-endpoint fallback, but stop dispatching when the
  // runtime reports pressure. Already running reads are allowed to finish.
  const loaded: string[] = []
  let next = 0
  let busy: any
  let unavailable: any
  const worker = async () => {
    while (current() && !busy && !unavailable && next < options.fallback.length) {
      const item = options.fallback[next++]!
      try {
        const result = await item.load()
        if (!current()) return
        if (result?.code === 200 && !result?.stale_session) loaded.push(item.key)
        else if (isBusy(result)) busy = result
        else if (isTransportUnavailable(result)) unavailable = result
      } catch (error) {
        if (isBusy(error)) busy = error
        else if (isTransportUnavailable(error)) unavailable = error
      }
    }
  }
  await Promise.all(Array.from({ length: FALLBACK_CONCURRENCY }, worker))
  if (!current()) return cancelled()
  return {
    kind: busy ? 'busy' : unavailable ? 'unavailable' : 'fallback',
    loaded, error: busy ?? unavailable ?? failure,
  }
}
