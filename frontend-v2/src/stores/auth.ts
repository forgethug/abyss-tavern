import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import apiClient, { type ApiEnvelope } from '@/api/client'
import type { User } from '@/types/platform'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null)
  const loading = ref(false)
  const isAuthenticated = computed(() => Boolean(user.value))

  async function login(username: string, password: string) {
    const response = await apiClient.post<ApiEnvelope<{ token: string; user: User }>>('/auth/login', {
      username,
      password,
    })
    localStorage.setItem('ai-rpg-token', response.data.data.token)
    user.value = response.data.data.user
  }

  async function register(username: string, password: string, displayName: string) {
    const response = await apiClient.post<ApiEnvelope<{ token: string; user: User }>>('/auth/register', {
      username,
      password,
      display_name: displayName || undefined,
    })
    localStorage.setItem('ai-rpg-token', response.data.data.token)
    user.value = response.data.data.user
  }

  async function loadMe() {
    if (!localStorage.getItem('ai-rpg-token')) return
    loading.value = true
    try {
      const response = await apiClient.get<ApiEnvelope<User>>('/auth/me')
      user.value = response.data.data
    } catch {
      logout()
    } finally {
      loading.value = false
    }
  }

  function logout() {
    localStorage.removeItem('ai-rpg-token')
    user.value = null
  }

  return { user, loading, isAuthenticated, login, register, loadMe, logout }
})

