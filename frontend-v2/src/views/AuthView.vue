<script setup lang="ts">
import axios from 'axios'
import { ref } from 'vue'
import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const mode = ref<'login' | 'register'>('login')
const username = ref('')
const displayName = ref('')
const password = ref('')
const error = ref('')
const submitting = ref(false)

async function submit() {
  error.value = ''
  submitting.value = true
  try {
    if (mode.value === 'login') {
      await auth.login(username.value, password.value)
    } else {
      await auth.register(username.value, password.value, displayName.value)
    }
    await router.push({ name: 'home' })
  } catch (caught) {
    error.value = axios.isAxiosError(caught)
      ? caught.response?.data?.message || '请求失败'
      : '请求失败'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="auth-layout">
    <div class="auth-copy">
      <p class="eyebrow">人工智能角色扮演平台</p>
      <h1>每一扇门后，<br />都是独立运行的世界。</h1>
      <p>同一个账号可以受邀进入不同酒馆；角色、剧情、任务与 AI 上下文彼此隔离。</p>
    </div>
    <form class="panel auth-panel" @submit.prevent="submit">
      <div class="tab-row">
        <button
          type="button"
          :class="{ active: mode === 'login' }"
          @click="mode = 'login'"
        >
          登录
        </button>
        <button
          type="button"
          :class="{ active: mode === 'register' }"
          @click="mode = 'register'"
        >
          注册
        </button>
      </div>
      <label>
        用户名
        <input v-model.trim="username" autocomplete="username" required />
      </label>
      <label v-if="mode === 'register'">
        显示名称
        <input v-model.trim="displayName" autocomplete="nickname" />
      </label>
      <label>
        密码
        <input
          v-model="password"
          type="password"
          :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
          required
        />
      </label>
      <p v-if="error" class="error-message">{{ error }}</p>
      <button class="button button-primary" type="submit" :disabled="submitting">
        {{ submitting ? '处理中…' : mode === 'login' ? '进入平台' : '建立账号' }}
      </button>
    </form>
  </section>
</template>
