<script setup lang="ts">
import axios from 'axios'
import { computed, onMounted, ref } from 'vue'

import apiClient, { type ApiEnvelope } from '@/api/client'
import NarrativeAdminPanel from '@/components/NarrativeAdminPanel.vue'
import type { GamePackDefinition, Tavern, User } from '@/types/platform'

const users = ref<User[]>([])
const taverns = ref<Tavern[]>([])
const gamePacks = ref<GamePackDefinition[]>([])
const displayName = ref('')
const slug = ref('')
const selectedGamePack = ref('')
const createIsPublic = ref(false)
const createAllowSelfJoin = ref(false)
const selectedTavernId = ref<number | null>(null)
const selectedUserId = ref<number | null>(null)
const message = ref('')
const error = ref('')
const runtimeActionLoading = ref<Record<number, string>>({})
const accessActionLoading = ref<Record<number, boolean>>({})
const narrativeTavernId = ref<number | null>(null)

async function loadData() {
  const [usersResponse, tavernsResponse, gamePacksResponse] = await Promise.all([
    apiClient.get<ApiEnvelope<User[]>>('/admin/users'),
    apiClient.get<ApiEnvelope<Tavern[]>>('/taverns'),
    apiClient.get<ApiEnvelope<GamePackDefinition[]>>('/admin/game-packs'),
  ])
  users.value = usersResponse.data.data
  taverns.value = tavernsResponse.data.data
  gamePacks.value = gamePacksResponse.data.data
  if (
    !selectedGamePack.value
    || !gamePacks.value.some((pack) => pack.pack_id === selectedGamePack.value)
  ) {
    selectedGamePack.value = gamePacks.value[0]?.pack_id || ''
  }
  if (
    narrativeTavernId.value === null
    || !taverns.value.some(
      (tavern) =>
        tavern.id === narrativeTavernId.value
        && tavern.status === 'active',
    )
  ) {
    narrativeTavernId.value = (
      taverns.value.find((tavern) => tavern.status === 'active')?.id
      || null
    )
  }
}

const narrativeTavern = computed(
  () => taverns.value.find(
    (tavern) => tavern.id === narrativeTavernId.value,
  ) || null,
)

async function createTavern() {
  clearNotice()
  try {
    await apiClient.post('/admin/taverns', {
      display_name: displayName.value,
      slug: slug.value,
      game_pack: selectedGamePack.value,
      is_public: createIsPublic.value,
      allow_self_join: createAllowSelfJoin.value,
    })
    displayName.value = ''
    slug.value = ''
    createIsPublic.value = false
    createAllowSelfJoin.value = false
    message.value = '酒馆已开放'
    await loadData()
  } catch (caught) {
    setError(caught)
  }
}

async function addMember() {
  clearNotice()
  if (!selectedTavernId.value || !selectedUserId.value) {
    error.value = '请选择酒馆和用户'
    return
  }
  try {
    await apiClient.post(`/admin/taverns/${selectedTavernId.value}/members`, {
      user_id: selectedUserId.value,
      role: 'member',
    })
    message.value = '成员已加入酒馆'
  } catch (caught) {
    setError(caught)
  }
}

async function setUserActive(user: User, isActive: boolean) {
  clearNotice()
  try {
    await apiClient.put(`/admin/users/${user.id}/status`, {
      is_active: isActive,
    })
    message.value = isActive ? '用户已启用' : '用户已停用'
    await loadData()
  } catch (caught) {
    setError(caught)
  }
}

async function setTavernStatus(tavern: Tavern, status: 'active' | 'disabled') {
  clearNotice()
  try {
    await apiClient.put(`/admin/taverns/${tavern.id}`, { status })
    message.value = status === 'active' ? '酒馆已开放' : '酒馆已停用并停止 Runtime'
    await loadData()
  } catch (caught) {
    setError(caught)
  }
}

async function setTavernAccess(
  tavern: Tavern,
  settings: { is_public?: boolean; allow_self_join?: boolean },
) {
  clearNotice()
  accessActionLoading.value = {
    ...accessActionLoading.value,
    [tavern.id]: true,
  }
  try {
    await apiClient.put(`/admin/taverns/${tavern.id}`, settings)
    message.value = '游戏公开与加入设置已更新'
    await loadData()
  } catch (caught) {
    setError(caught)
  } finally {
    const nextLoading = { ...accessActionLoading.value }
    delete nextLoading[tavern.id]
    accessActionLoading.value = nextLoading
  }
}

function tavernStatusLabel(status: Tavern['status']) {
  return {
    draft: '筹备中',
    active: '已开放',
    disabled: '已停用',
  }[status]
}

function runtimeStatusLabel(status?: string) {
  const labels: Record<string, string> = {
    pending: '等待启动',
    starting: '正在启动',
    ready: '运行中',
    stopped: '已停止',
    failed: '启动失败',
  }
  return labels[status || 'pending'] || '状态未知'
}

function gamePackLabel(packId: string) {
  return gamePacks.value.find((pack) => pack.pack_id === packId)?.display_name
    || '独立游戏世界'
}

async function controlTavernRuntime(
  tavern: Tavern,
  action: 'start' | 'stop' | 'restart',
) {
  clearNotice()
  runtimeActionLoading.value = {
    ...runtimeActionLoading.value,
    [tavern.id]: action,
  }
  try {
    await apiClient.post(`/admin/taverns/${tavern.id}/runtime/${action}`)
    const actionLabels = {
      start: '启动',
      stop: '停止',
      restart: '重启',
    }
    message.value = `${tavern.display_name} Runtime 已${actionLabels[action]}`
    await loadData()
  } catch (caught) {
    setError(caught)
  } finally {
    const nextLoading = { ...runtimeActionLoading.value }
    delete nextLoading[tavern.id]
    runtimeActionLoading.value = nextLoading
  }
}

function clearNotice() {
  error.value = ''
  message.value = ''
}

function setError(caught: unknown) {
  error.value = axios.isAxiosError(caught)
    ? caught.response?.data?.message || '操作失败'
    : '操作失败'
}

onMounted(loadData)
</script>

<template>
  <section>
    <div class="hero compact">
      <p class="eyebrow">平台控制</p>
      <h1>酒馆管理</h1>
      <p>开放你的深渊酒馆，管理玩家加入方式与运行状态。</p>
    </div>
    <details class="panel admin-start-guide">
      <summary>首次开服与管理员配置说明</summary>
      <ol>
        <li>在下方创建酒馆。勾选“公开展示”让玩家在首页看到；勾选“允许玩家自由加入”让他们直接加入。</li>
        <li>进入酒馆后，玩家在“我的”提交角色预约。管理员从“建筑 → 预约管理”查看床位并执行抽取。</li>
        <li>酒馆内“管理 → 探险 → 运行与 AI”可调整组队、发车和停用时段；管理台顶部可控制 AI 总闸。</li>
        <li>更换接口、Key 或模型：进入目标酒馆的「管理 → 探险 → 运行与 AI」，在模型配置中保存；可按用途分配，修改只影响当前酒馆，无需重启。</li>
      </ol>
      <p>完整步骤见源码中的 <code>docs/ADMIN.md</code>（英文：<code>docs/ADMIN.en.md</code>）。普通注册账号没有管理权限，请使用安装向导创建的管理员账号。</p>
    </details>
    <p v-if="message" class="success-message">{{ message }}</p>
    <p v-if="error" class="error-message">{{ error }}</p>
    <div class="admin-grid">
      <form class="panel form-stack" @submit.prevent="createTavern">
        <h2>开放新酒馆</h2>
        <label>
          对外名称
          <input v-model.trim="displayName" placeholder="例如：灰烬与星火" required />
        </label>
        <label>
          访问标识
          <input v-model.trim="slug" placeholder="例如：ash-and-ember" required />
        </label>
        <label>
          游戏包
          <select v-model="selectedGamePack" required>
            <option
              v-for="pack in gamePacks"
              :key="pack.pack_id"
              :value="pack.pack_id"
            >
              {{ pack.display_name }} · {{ pack.pack_version }}
            </option>
          </select>
        </label>
        <label class="inline-choice">
          <input
            v-model="createIsPublic"
            type="checkbox"
            @change="!createIsPublic && (createAllowSelfJoin = false)"
          />
          公开展示（所有玩家都能在世界列表中发现）
        </label>
        <label class="inline-choice">
          <input
            v-model="createAllowSelfJoin"
            type="checkbox"
            :disabled="!createIsPublic"
          />
          允许玩家自由加入
        </label>
        <button class="button button-primary" type="submit">开放酒馆</button>
      </form>
      <form class="panel form-stack" @submit.prevent="addMember">
        <h2>分配成员</h2>
        <label>
          酒馆
          <select v-model="selectedTavernId" required>
            <option :value="null" disabled>选择酒馆</option>
            <option v-for="tavern in taverns" :key="tavern.id" :value="tavern.id">
              {{ tavern.display_name }}
            </option>
          </select>
        </label>
        <label>
          用户
          <select v-model="selectedUserId" required>
            <option :value="null" disabled>选择用户</option>
            <option v-for="user in users" :key="user.id" :value="user.id">
              {{ user.display_name }}（{{ user.username }}）
            </option>
          </select>
        </label>
        <button class="button button-primary" type="submit">加入酒馆</button>
      </form>
    </div>
    <div class="admin-grid management-grid">
      <section class="panel">
        <h2>用户管理</h2>
        <div class="management-list">
          <article v-for="user in users" :key="user.id" class="management-row">
            <div>
              <strong>{{ user.display_name }}</strong>
              <small>{{ user.username }} · {{ user.is_admin ? '管理员' : '玩家' }}</small>
            </div>
            <button
              class="button button-ghost"
              type="button"
              @click="setUserActive(user, user.is_active === false)"
            >
              {{ user.is_active === false ? '启用' : '停用' }}
            </button>
          </article>
        </div>
      </section>
      <section class="panel">
        <h2>运行进程与开放设置</h2>
        <div class="management-list">
          <article v-for="tavern in taverns" :key="tavern.id" class="management-row">
            <div>
              <strong>{{ tavern.display_name }}</strong>
              <small>
                {{ tavernStatusLabel(tavern.status) }}
                · 运行进程 {{ runtimeStatusLabel(tavern.runtime_status) }}
                <template v-if="tavern.runtime_pid"> · 进程号 {{ tavern.runtime_pid }}</template>
              </small>
              <small>
                {{ tavern.is_public ? '公开可发现' : '私人游戏' }}
                ·
                {{ tavern.allow_self_join ? '允许自由加入' : '仅限邀请加入' }}
              </small>
              <small v-if="tavern.runtime_error" class="error-message">
                {{ tavern.runtime_error }}
              </small>
            </div>
            <div class="management-actions">
              <button
                class="button button-ghost"
                type="button"
                :disabled="Boolean(accessActionLoading[tavern.id])"
                @click="
                  setTavernAccess(
                    tavern,
                    tavern.is_public
                      ? { is_public: false, allow_self_join: false }
                      : { is_public: true },
                  )
                "
              >
                {{ tavern.is_public ? '设为私人' : '公开展示' }}
              </button>
              <button
                class="button button-ghost"
                type="button"
                :disabled="
                  !tavern.is_public
                  || Boolean(accessActionLoading[tavern.id])
                "
                @click="
                  setTavernAccess(
                    tavern,
                    { allow_self_join: !tavern.allow_self_join },
                  )
                "
              >
                {{ tavern.allow_self_join ? '关闭自由加入' : '允许自由加入' }}
              </button>
              <button
                class="button button-ghost"
                type="button"
                :disabled="
                  tavern.status !== 'active'
                  || tavern.runtime_status === 'ready'
                  || Boolean(runtimeActionLoading[tavern.id])
                "
                @click="controlTavernRuntime(tavern, 'start')"
              >
                {{ runtimeActionLoading[tavern.id] === 'start' ? '启动中…' : '启动' }}
              </button>
              <button
                class="button button-ghost"
                type="button"
                :disabled="Boolean(runtimeActionLoading[tavern.id])"
                @click="controlTavernRuntime(tavern, 'stop')"
              >
                {{ runtimeActionLoading[tavern.id] === 'stop' ? '停止中…' : '停止' }}
              </button>
              <button
                class="button button-ghost"
                type="button"
                :disabled="
                  tavern.status !== 'active'
                  || Boolean(runtimeActionLoading[tavern.id])
                "
                @click="controlTavernRuntime(tavern, 'restart')"
              >
                {{ runtimeActionLoading[tavern.id] === 'restart' ? '重启中…' : '重启' }}
              </button>
              <button
                class="button button-ghost"
                type="button"
                :disabled="Boolean(runtimeActionLoading[tavern.id])"
                @click="setTavernStatus(tavern, tavern.status === 'active' ? 'disabled' : 'active')"
              >
                {{ tavern.status === 'active' ? '停用酒馆' : '重新开放' }}
              </button>
            </div>
          </article>
        </div>
      </section>
    </div>
    <section class="panel narrative-picker">
      <div>
        <p class="eyebrow">世界剧情管理</p>
        <h2>共享世界叙事层</h2>
        <p>
          统一查看各酒馆的实体、事实、故事线、审核候选、注入快照与版本来源。
        </p>
      </div>
      <label>
        管理酒馆
        <select v-model="narrativeTavernId">
          <option :value="null" disabled>选择已开放酒馆</option>
          <option
            v-for="tavern in taverns.filter((item) => item.status === 'active')"
            :key="tavern.id"
            :value="tavern.id"
          >
              {{ tavern.display_name }} · {{ gamePackLabel(tavern.game_pack) }}
          </option>
        </select>
      </label>
    </section>
    <NarrativeAdminPanel
      v-if="narrativeTavern"
      :key="narrativeTavern.id"
      :tavern-id="narrativeTavern.id"
      :tavern-name="narrativeTavern.display_name"
      :tavern-slug="narrativeTavern.slug"
    />
  </section>
</template>

<style scoped>
.admin-start-guide { margin-bottom: 24px; padding: 18px 22px; color: #c8c3cf; line-height: 1.8; }
.admin-start-guide summary { cursor: pointer; font-weight: 600; color: #ece9df; }
.admin-start-guide li + li { margin-top: 8px; }
.admin-start-guide code { overflow-wrap: anywhere; }
.management-grid {
  margin-top: 20px;
}

.management-list {
  display: grid;
  gap: 10px;
}

.management-row {
  align-items: center;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  display: flex;
  gap: 16px;
  justify-content: space-between;
  padding: 10px 0;
}

.management-row div {
  display: grid;
  gap: 4px;
}

.management-row small {
  color: var(--muted);
}

.management-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: flex-end;
}

.narrative-picker {
  align-items: end;
  display: grid;
  gap: 20px;
  grid-template-columns: minmax(0, 1fr) minmax(240px, 360px);
  margin-top: 20px;
}

.narrative-picker h2,
.narrative-picker p {
  margin-bottom: 0;
}

.narrative-picker label {
  display: grid;
  gap: 6px;
}

@media (max-width: 760px) {
  .narrative-picker {
    grid-template-columns: 1fr;
  }
}
</style>
