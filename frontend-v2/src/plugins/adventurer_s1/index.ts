import type { PluginInstance } from '@/core/types'
import routes from './routes'

const adventurerS1Plugin: PluginInstance = {
  manifest: {
    name: 'adventurer_s1',
    displayName: '深渊酒馆S1',
    version: '0.1.0',
    icon: '⚔️',
    routes,
    navItems: [
      { label: '深渊酒馆S1', icon: '⚔️', path: '/adventurer-s1', order: 111 },
    ],
  },
  install(router: any, _pinia: any, routesToRegister?: any[]) {
    for (const route of (routesToRegister ?? routes)) {
      router.addRoute(route)
    }
  },
  uninstall() {},
}

export default adventurerS1Plugin
