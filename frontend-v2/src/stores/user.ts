import { defineStore } from 'pinia'
import { computed } from 'vue'

import { useAuthStore } from '@/stores/auth'


export const useUserStore = defineStore('legacy-user-compat', () => {
  const auth = useAuthStore()
  const user = computed(() => {
    if (!auth.user) return null
    return {
      id: auth.user.id,
      username: auth.user.username,
      nickname: auth.user.display_name,
      is_admin: auth.user.is_admin,
      balance: 0,
    }
  })

  function updateBalance(_newBalance: number) {
    // S1 owns its currencies; no stock-platform balance is projected.
  }

  return { user, updateBalance }
})
