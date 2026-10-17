import { createRouter, createWebHistory } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { installTavernNavigation } from '@/runtime/tavernNavigation'

const AdminView = () => import('@/views/AdminView.vue')
const AuthView = () => import('@/views/AuthView.vue')
const HomeView = () => import('@/views/HomeView.vue')
const TavernRuntimeView = () => import('@/views/TavernRuntimeView.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'home', component: HomeView, meta: { requiresAuth: true } },
    { path: '/auth', name: 'auth', component: AuthView },
    { path: '/admin', name: 'admin', component: AdminView, meta: { requiresAuth: true, admin: true } },
    {
      path: '/play/:slug',
      name: 'tavern-runtime',
      component: TavernRuntimeView,
      meta: { requiresAuth: true },
    },
  ],
})

installTavernNavigation(router)

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.user && localStorage.getItem('ai-rpg-token')) {
    await auth.loadMe()
  }
  if (to.meta.requiresAuth && !auth.isAuthenticated) return { name: 'auth' }
  if (to.meta.admin && !auth.user?.is_admin) return { name: 'home' }
  if (to.name === 'auth' && auth.isAuthenticated) return { name: 'home' }
})

export default router
