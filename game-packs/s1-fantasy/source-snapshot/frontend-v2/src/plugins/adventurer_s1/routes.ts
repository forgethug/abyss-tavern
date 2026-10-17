import type { PluginRoute } from '@/core/types'

const routes: PluginRoute[] = [
  {
    path: '/adventurer-s1',
    name: 'adventurer_s1',
    component: () => import('./views/AdventurerS1View.vue'),
    meta: {
      title: '深渊酒馆S1',
      icon: '⚔️',
      requiresAuth: true,
      navbar: true,
      order: 111,
      mobileEdgeToEdge: true,
    },
  },
]

export default routes
