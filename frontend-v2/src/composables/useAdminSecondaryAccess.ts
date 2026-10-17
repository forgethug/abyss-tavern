import { computed } from 'vue'

import {
  getAdminSecondaryAuthScope,
  getAdminSecondaryAuthStatusReady,
} from '@/composables/useApi'
import { useUserStore } from '@/stores/user'


export function useAdminSecondaryAccess() {
  const userStore = useUserStore()
  const isAdminAccount = computed(() => Boolean(userStore.user?.is_admin))
  const canUseFullAdminUi = computed(() => isAdminAccount.value)
  const scope = getAdminSecondaryAuthScope()
  const statusReady = getAdminSecondaryAuthStatusReady()

  return {
    isAdminAccount,
    canUseFullAdminUi,
    scope,
    statusReady,
  }
}
