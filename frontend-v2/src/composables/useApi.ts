import axios, { type AxiosInstance } from 'axios'
import { readonly, ref } from 'vue'

import { tavernLifecycle, type TavernSession } from '@/runtime/activeTavern'


// Upstream UI request metadata remains source-compatible. In the portable
// platform the selected tavern is the routing authority for every S1 request.
export type S1AdminRealmTarget = 'region_1' | 'region_2'
export type S1PlayerRealmView = 'region_1' | 'region_2'
export type S1RealmContext = 'selected' | 'membership'

declare module 'axios' {
  interface AxiosRequestConfig<D = any> {
    s1RealmContext?: S1RealmContext
    s1AdminRealmTarget?: S1AdminRealmTarget
    tavernSession?: TavernSession
  }
}

export function getS1AdminRealmTarget(): S1AdminRealmTarget {
  return 'region_1'
}

export function getS1PlayerRealmView(): S1PlayerRealmView | null {
  return 'region_1'
}

const api: AxiosInstance = axios.create({
  baseURL: '/api',
  timeout: 30_000,
})

const adminSecondaryAuthScope = readonly(ref<'full'>('full'))
const adminSecondaryAuthStatusReady = readonly(ref(true))
const adminSecondarySocketReadGrant = readonly(ref<string | null>(null))

export function getAdminSecondarySocketReadGrant() {
  return adminSecondarySocketReadGrant
}

export function getAdminSecondaryAuthScope() {
  return adminSecondaryAuthScope
}

export function getAdminSecondaryAuthStatusReady() {
  return adminSecondaryAuthStatusReady
}

export async function syncAdminSecondaryAuthStatus() {
  // The platform control plane is the sole administrator authorization gate.
}

api.interceptors.request.use((config) => {
  const session = tavernLifecycle.requireCurrent()
  config.tavernSession = session
  config.headers.Authorization = `Bearer ${session.token}`
  config.signal = config.signal
    ? AbortSignal.any([session.signal, config.signal as AbortSignal])
    : session.signal
  if (config.url?.startsWith('/adventurer-s1')) {
    config.url = `/taverns/${encodeURIComponent(session.slug)}/runtime${config.url.slice('/adventurer-s1'.length)}`
  }
  delete config.s1RealmContext
  delete config.s1AdminRealmTarget
  return config
}, (error) => { throw error }, { synchronous: true })

api.interceptors.response.use(
  (response) => {
    const session = response.config.tavernSession
    if (!session || !tavernLifecycle.isCurrent(session)) {
      throw new axios.CanceledError('酒馆会话已结束')
    }
    return response
  },
  (error) => {
    const session = error.config?.tavernSession
    if (!session || !tavernLifecycle.isCurrent(session)) {
      return Promise.reject(new axios.CanceledError('酒馆会话已结束'))
    }
    if (error.response?.status === 401) {
      localStorage.removeItem('ai-rpg-token')
      tavernLifecycle.end()
    }
    return Promise.reject(error)
  },
)

export function useApi() {
  return api
}

export function prefetchChallengeToken() {
  // The new control plane performs authentication and membership checks at the edge.
}

export default api
