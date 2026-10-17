<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const isGamePage = computed(() => route.path.startsWith('/play/'))
const toast = ref<{ kind: string; message: string } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

function onMessage(event: Event) {
  const detail = (event as CustomEvent).detail
  toast.value = {
    kind: detail?.kind || 'info',
    message: String(detail?.message || ''),
  }
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => {
    toast.value = null
  }, 3200)
}

function logout() {
  auth.logout()
  router.push({ name: 'auth' })
}

onMounted(() => window.addEventListener('ai-rpg-message', onMessage))
onBeforeUnmount(() => {
  window.removeEventListener('ai-rpg-message', onMessage)
  if (toastTimer) clearTimeout(toastTimer)
})
</script>

<template>
  <div class="app-shell" :class="{ 'app-shell--game': isGamePage }">
    <header class="topbar">
      <RouterLink class="brand" to="/">深渊酒馆</RouterLink>
      <nav v-if="auth.user" class="topbar-actions">
        <span class="user-name">{{ auth.user.display_name }}</span>
        <RouterLink v-if="auth.user.is_admin" class="text-link" to="/admin">管理后台</RouterLink>
        <button class="button button-ghost" type="button" @click="logout">退出</button>
      </nav>
    </header>
    <main class="page-shell" :class="{ 'page-shell--game': isGamePage }">
      <RouterView />
    </main>
    <div v-if="toast" class="global-toast" :data-kind="toast.kind">
      {{ toast.message }}
    </div>
  </div>
</template>

<style scoped>
.global-toast {
  background: #282331;
  border: 1px solid #8a7750;
  border-radius: 10px;
  bottom: 24px;
  box-shadow: 0 12px 36px rgba(0, 0, 0, 0.45);
  color: #f4ead3;
  left: 50%;
  max-width: min(520px, calc(100vw - 32px));
  padding: 12px 18px;
  position: fixed;
  transform: translateX(-50%);
  z-index: 10000;
}

.global-toast[data-kind='error'] {
  border-color: #b45c67;
}

.global-toast[data-kind='success'] {
  border-color: #5e9a73;
}
</style>
