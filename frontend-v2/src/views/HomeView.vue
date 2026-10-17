<script setup lang="ts">
import axios from 'axios'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import apiClient, { type ApiEnvelope } from '@/api/client'
import type { Tavern } from '@/types/platform'

const router = useRouter()
const taverns = ref<Tavern[]>([])
const loading = ref(true)
const joiningSlug = ref('')
const error = ref('')

const gamePackLabels: Record<string, string> = {
  s1_fantasy: '深渊酒馆',
}

const membershipLabels: Record<string, string> = {
  owner: '创建者',
  manager: '管理员',
  member: '成员',
  platform_admin: '平台管理员',
}

async function loadTaverns() {
  try {
    const response = await apiClient.get<ApiEnvelope<Tavern[]>>('/taverns')
    taverns.value = response.data.data
  } finally {
    loading.value = false
  }
}

async function joinTavern(tavern: Tavern) {
  joiningSlug.value = tavern.slug
  error.value = ''
  try {
    await apiClient.post(`/taverns/${encodeURIComponent(tavern.slug)}/join`)
    await loadTaverns()
    await router.push(`/play/${tavern.slug}`)
  } catch (caught) {
    error.value = axios.isAxiosError(caught)
      ? caught.response?.data?.message || '加入游戏失败'
      : '加入游戏失败'
  } finally {
    joiningSlug.value = ''
  }
}

onMounted(loadTaverns)
</script>

<template>
  <section>
    <div class="hero">
      <p class="eyebrow">我的世界</p>
      <h1>选择今晚要推开的门</h1>
      <p>这里显示你已加入和公开可发现的游戏。不同世界不会共享角色、剧情与任务状态。</p>
    </div>
    <p v-if="error" class="error-message">{{ error }}</p>
    <div v-if="loading" class="empty-state">正在读取酒馆…</div>
    <div v-else-if="!taverns.length" class="panel empty-state">
      当前没有可加入的公开游戏，也没有收到私人邀请。
    </div>
    <div v-else class="card-grid">
      <article v-for="tavern in taverns" :key="tavern.id" class="tavern-card">
        <div class="card-glow" />
        <p class="card-kicker">{{ gamePackLabels[tavern.game_pack] || '独立游戏世界' }}</p>
        <h2>{{ tavern.display_name }}</h2>
        <p v-if="tavern.membership_role">
          身份：{{ membershipLabels[tavern.membership_role] || tavern.membership_role }}
        </p>
        <p v-else-if="tavern.can_self_join">公开游戏 · 可自由加入</p>
        <p v-else>公开游戏 · 需要管理员邀请</p>
        <RouterLink
          v-if="tavern.membership_role"
          class="button button-primary"
          :to="`/play/${tavern.slug}`"
        >
          进入游戏
        </RouterLink>
        <button
          v-if="!tavern.membership_role && tavern.can_self_join"
          class="button button-primary"
          type="button"
          :disabled="joiningSlug === tavern.slug"
          @click="joinTavern(tavern)"
        >
          {{ joiningSlug === tavern.slug ? '正在加入…' : '自由加入并进入' }}
        </button>
        <span v-else-if="!tavern.membership_role" class="status-chip">
          等待管理员邀请
        </span>
      </article>
    </div>
  </section>
</template>
