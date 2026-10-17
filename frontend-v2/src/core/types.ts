import type { RouteRecordRaw } from 'vue-router'


export type PluginRoute = RouteRecordRaw

export interface PluginInstance {
  manifest: {
    name: string
    displayName: string
    version: string
    icon?: string
    routes: PluginRoute[]
    navItems?: Array<{
      label: string
      icon?: string
      path: string
      order?: number
    }>
  }
  install: (...args: any[]) => void
  uninstall: () => void
}
