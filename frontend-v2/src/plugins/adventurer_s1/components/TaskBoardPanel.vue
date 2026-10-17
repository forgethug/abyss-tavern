<template>
  <div class="task-board-panel">
    <div v-if="isAdmin" class="task-board-toolbar">
      <button type="button" class="btn-task-admin" @click="showAdmin = true">⚙️ 任务管理</button>
    </div>
    <!-- 进行中任务 -->
    <div v-if="activeTask" class="active-task-section">
      <h5 class="section-title">进行中的任务</h5>
      <div class="active-task-card">
        <div class="task-header">
          <span class="task-icon">{{ activeTask.template?.icon ?? '📜' }}</span>
          <div>
            <div class="task-name">{{ activeTask.template?.name ?? activeTask.task_template_id }}</div>
            <div class="task-adv-name">🧑 {{ activeAdvName }}</div>
          </div>
        </div>
        <div v-if="activeTask.template?.description" class="task-active-desc">
          {{ activeTask.template.description }}
        </div>
        <div v-if="isExpeditionActiveTask" class="task-expedition-hint">
          <div class="remaining-text">
            <template v-if="activeTask.no_damage || activeTask.template?.no_damage">
              🛡️ 目标：无伤通关任意副本
            </template>
            <template v-else-if="activeTask.required_random_solo_match || activeTask.template?.required_random_solo_match">
              🎲 目标：单人匹配「随机地图」并通关
            </template>
            <template v-else>
              🗺️ 目标：通关「{{ activeTask.required_dungeon ?? activeTask.required_dungeon_label ?? activeTask.template?.required_dungeon ?? activeTask.template?.required_dungeon_label }}」
            </template>
          </div>
          <p class="expedition-note">通关后将自动发放奖励，无需手动领取。</p>
        </div>
        <div v-else-if="isBuildingHelpActiveTask" class="task-building-help">
          <div class="progress-bar-lg">
            <div class="progress-fill-lg task-progress" :style="{ width: buildingHelpProgressPercent + '%' }"></div>
          </div>
          <div class="remaining-text">
            <template v-if="buildingHelpRemainingSeconds > 0">
              已帮工 {{ formatBuildingHelpProgress }} / {{ formatBuildingHelpRequired }}
            </template>
            <template v-else>✅ 帮工时长已达标</template>
          </div>
          <p class="expedition-note">接取后前往任意建造中的建筑点「派遣帮忙」，累计在场时长达标后可领取奖励。</p>
        </div>
        <div v-else-if="isGatheringActiveTask" class="task-gathering">
          <div class="progress-bar-lg">
            <div class="progress-fill-lg task-progress" :style="{ width: gatheringProgressPercent + '%' }"></div>
          </div>
          <div class="remaining-text">
            <template v-if="gatheringRemainingSeconds > 0">
              已采集 {{ formatGatheringProgress }} / {{ formatGatheringRequired }}
            </template>
            <template v-else>✅ 采集时长已达标</template>
          </div>
          <p class="expedition-note">接取后前往伐木林或采石场点「开始采集」，累计采集时长达标后可领取奖励。</p>
        </div>
        <div v-else-if="isWorkActiveTask" class="task-work">
          <div class="progress-bar-lg">
            <div class="progress-fill-lg task-progress" :style="{ width: workProgressPercent + '%' }"></div>
          </div>
          <div class="remaining-text">
            <template v-if="workRemainingSessions > 0">
              已完成 {{ workProgressSessions }} / {{ workRequiredSessions }} 次打工
            </template>
            <template v-else>✅ 打工次数已达标</template>
          </div>
          <p class="expedition-note">接取后前往打工区任意岗位点「开始打工」，完成一次工作后可领取奖励。</p>
        </div>
        <div v-else-if="isContributionActiveTask" class="task-contribution">
          <div class="progress-bar-lg">
            <div class="progress-fill-lg task-progress" :style="{ width: contributionProgressPercent + '%' }"></div>
          </div>
          <div class="remaining-text">
            <template v-if="contributionRemaining > 0">
              已获得 {{ contributionProgress }} / {{ contributionRequired }} 贡献
            </template>
            <template v-else>✅ 贡献值已达标</template>
          </div>
          <p class="expedition-note">接取后通过建造帮工或捐献材料累计获得贡献值，达标后可领取奖励。</p>
        </div>
        <div v-else-if="isTreasureHuntActiveTask" class="task-treasure-hunt">
          <div class="remaining-text">🎟️ 搜寻中…帮工或采集中有小概率随机掉落一张机缘门票</div>
          <p class="expedition-note">前往建造工地点「派遣帮忙」，或前往伐木林、采石场、铁矿山、炭窑、熔铁坊开始采集。每次命中随机掉落一张门票，全服共 5 张。</p>
        </div>
        <div v-else-if="isHiddenBossActiveTask" class="task-hidden-boss">
          <div class="progress-bar-lg">
            <div class="progress-fill-lg task-progress" :style="{ width: hiddenBossProgressPercent + '%' }"></div>
          </div>
          <div class="remaining-text">
            👑 全服隐藏 Boss 进度 {{ hiddenBossProgressCount }} / {{ hiddenBossTotalCount }}
          </div>
        </div>
        <div v-else class="task-timer">
          <div class="progress-bar-lg">
            <div class="progress-fill-lg task-progress" :style="{ width: taskProgressPercent + '%' }"></div>
          </div>
          <div class="remaining-text">
            <template v-if="taskRemainingSeconds > 0">剩余：{{ formatTaskRemaining }}</template>
            <template v-else>✅ 任务完成</template>
          </div>
        </div>
        <button
          v-if="!isExpeditionActiveTask && !isTreasureHuntActiveTask && !isHiddenBossActiveTask && canClaimTaskReward"
          type="button"
          class="advs1-submit task-complete-btn"
          :disabled="completing"
          @click="completeTask"
        >
          {{ completing ? '结算中…' : '🎁 领取奖励' }}
        </button>
        <button
          type="button"
          class="advs1-submit task-abandon-btn"
          :disabled="abandoning"
          @click="abandonTask"
        >
          {{ abandoning ? '处理中…' : '放弃任务' }}
        </button>
      </div>
    </div>

    <!-- 空闲态：任务列表 -->
    <div v-else class="task-list-section">
      <h5 class="section-title">任务列表</h5>
      <div v-if="loading" class="empty-helpers">加载中…</div>
      <div v-else-if="displayTasks.length === 0" class="empty-helpers">暂无可用任务</div>
      <div v-else class="task-list">
        <div
          v-for="task in displayTasks"
          :key="task.id"
          class="task-card"
          :class="{ disabled: !task.can_accept, ended: task.is_ended }"
          @click="onTaskClick(task)"
        >
          <div class="task-card-header">
            <span class="task-icon">{{ task.icon }}</span>
            <div class="task-card-info">
              <div class="task-name">{{ task.name }}</div>
              <div class="task-category">{{ categoryLabel(task.category) }}</div>
            </div>
            <div class="task-duration">
              <template v-if="task.completion_type === 'expedition' && task.no_damage">
                🛡️ 无伤任意副本
              </template>
              <template v-else-if="task.completion_type === 'expedition' && task.required_random_solo_match">
                🎲 单排随机地图
              </template>
              <template v-else-if="task.completion_type === 'expedition'">
                🗺️ {{ task.required_dungeon ?? task.required_dungeon_label }}
              </template>
              <template v-else-if="task.completion_type === 'building_help'">
                🔨 帮工 {{ task.duration_minutes }} 分钟
              </template>
              <template v-else-if="task.completion_type === 'gathering'">
                🪓 采集 {{ task.duration_minutes }} 分钟
              </template>
              <template v-else-if="task.completion_type === 'work'">
                💼 打工 {{ task.work_required_sessions ?? 1 }} 次
              </template>
              <template v-else-if="task.completion_type === 'contribution'">
                ⭐ 贡献 {{ task.contribution_required ?? 100 }}
              </template>
              <template v-else-if="task.completion_type === 'treasure_hunt'">
                🎟️ 限时掉落
              </template>
              <template v-else-if="task.completion_type === 'hidden_boss'">
                👑 隐藏 Boss 讨伐
              </template>
              <template v-else>
                ⏱️ {{ task.duration_minutes }}分钟
              </template>
            </div>
          </div>
          <div class="task-desc">{{ task.description }}</div>
          <div v-if="task.max_completions != null" class="task-quota">
            <template v-if="task.is_ended">已结束（{{ task.completion_count ?? 0 }}/{{ task.max_completions }} 已完成）</template>
            <template v-else-if="task.completion_type === 'treasure_hunt'">剩余门票掉落：{{ task.completion_remaining ?? 0 }} / {{ task.max_completions }}</template>
            <template v-else-if="task.completion_type === 'hidden_boss'">隐藏Boss进度：{{ task.hidden_boss_progress_count ?? task.completion_count ?? 0 }} / {{ task.hidden_boss_total_count ?? task.max_completions }}</template>
            <template v-else>剩余名额：{{ task.completion_remaining ?? 0 }} / {{ task.max_completions }}</template>
          </div>
          <div class="task-meta">
            <div class="task-requirements">
              <span v-if="task.requirements.min_level">Lv.{{ task.requirements.min_level }}+</span>
              <span v-if="task.requirements.required_stat && task.requirements.required_stat_min">
                {{ statLabel(task.requirements.required_stat) }}≥{{ task.requirements.required_stat_min }}
              </span>
              <span v-if="task.requirements.stamina_cost">💪{{ task.requirements.stamina_cost }}</span>
            </div>
            <div class="task-rewards">
              <span v-if="task.rewards.gold">💰{{ task.rewards.gold }}</span>
              <span v-if="task.rewards.exp">✨{{ task.rewards.exp }}</span>
              <template v-if="task.rewards.materials">
                <span v-for="(amt, mat) in task.rewards.materials" :key="mat">{{ materialIcon(mat) }}+{{ amt }}</span>
              </template>
              <template v-if="task.rewards.stat">
                <span v-for="(amt, stat) in task.rewards.stat" :key="stat">{{ statLabel(stat) }}+{{ amt }}</span>
              </template>
              <template v-if="task.rewards?.items && (task.reward_drop_mode === 'random_one' || task.rewards.drop_mode === 'random_one' || task.completion_type === 'treasure_hunt')">
                <span class="task-reward-treasure-line">
                  <span class="task-reward-treasure-summary">🎟️ 随机×1</span>
                  <span class="task-reward-pool-sep">·</span>
                  <span class="task-reward-pool">
                    <template v-for="(entry, idx) in task.rewards.items" :key="idx">
                      <span v-if="idx > 0" class="task-reward-pool-sep">/</span>
                      <span
                        class="task-reward-item-name task-reward-preview-trigger"
                        :style="taskRewardItemRarity(entry) ? { color: rarityColor(taskRewardItemRarity(entry)) } : undefined"
                        role="button"
                        tabindex="0"
                        @click.stop="openRewardPreview(entry)"
                        @keydown.enter.stop="openRewardPreview(entry)"
                        @keydown.space.prevent.stop="openRewardPreview(entry)"
                      >{{ taskRewardPoolItemName(entry) }}</span>
                    </template>
                  </span>
                </span>
              </template>
              <template v-else-if="task.rewards.items">
                <span
                  v-for="(entry, idx) in task.rewards.items"
                  :key="idx"
                  class="task-reward-item"
                >
                  🎁<span
                    class="task-reward-item-name task-reward-preview-trigger"
                    :style="taskRewardItemRarity(entry) ? { color: rarityColor(taskRewardItemRarity(entry)) } : undefined"
                    role="button"
                    tabindex="0"
                    @click.stop="openRewardPreview(entry)"
                    @keydown.enter.stop="openRewardPreview(entry)"
                    @keydown.space.prevent.stop="openRewardPreview(entry)"
                  >{{ taskRewardItemLabel(entry) }}</span><span
                    v-if="taskRewardItemRarity(entry)"
                    class="rarity-pill"
                    :style="{
                      color: rarityColor(taskRewardItemRarity(entry)),
                      borderColor: rarityColor(taskRewardItemRarity(entry)),
                    }"
                  >{{ rarityLabel(taskRewardItemRarity(entry)!) }}</span>
                </span>
              </template>
            </div>
          </div>
          <div v-if="!task.can_accept && task.reject_reason" class="task-reject-reason">
            {{ task.reject_reason }}
          </div>
        </div>
      </div>
    </div>
    <TaskBoardAdminModal v-model:visible="showAdmin" @saved="loadData" />
    <RewardItemPreviewModal :item="rewardPreview" @close="closeRewardPreview" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useAdventurerS1Store, isPlayableAdventurer, type TaskTemplate, type TaskInstance, type BuildingInstance } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { useLiveTick } from '../composables/useLiveTick'
import { useSocket } from '@/composables/useSocket'
import { filterVisibleTaskTemplates } from '../utils/taskBoard'
import { rarityColor, rarityLabel } from '../utils'
import { type RewardPreviewItem } from '../itemTypes'
import TaskBoardAdminModal from './TaskBoardAdminModal.vue'
import RewardItemPreviewModal from './RewardItemPreviewModal.vue'
import { useAdminSecondaryAccess } from '@/composables/useAdminSecondaryAccess'

const props = defineProps<{
  building: BuildingInstance
}>()

const emit = defineEmits<{
  refreshed: []
}>()

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const socket = useSocket()
const { now } = useLiveTick()

const { canUseFullAdminUi: isAdmin } = useAdminSecondaryAccess()
const showAdmin = ref(false)

const loading = ref(false)
const tasks = ref<TaskTemplate[]>([])
const displayTasks = computed(() => filterVisibleTaskTemplates(tasks.value))
type AdventurerChangedPayload = {
  action?: string
  reason?: string
  ids?: number[]
  dropped_item?: string
}

const activeTask = ref<TaskInstance | null>(null)
const activeAdvName = ref('')
const completing = ref(false)
const abandoning = ref(false)
const accepting = ref(false)

const myAdventurer = computed(() =>
  store.adventurers.find(isPlayableAdventurer) ?? null,
)

const isExpeditionActiveTask = computed(() =>
  activeTask.value?.completion_type === 'expedition'
  || activeTask.value?.template?.completion_type === 'expedition',
)

const isBuildingHelpActiveTask = computed(() =>
  activeTask.value?.completion_type === 'building_help'
  || activeTask.value?.template?.completion_type === 'building_help',
)

const isGatheringActiveTask = computed(() =>
  activeTask.value?.completion_type === 'gathering'
  || activeTask.value?.template?.completion_type === 'gathering',
)

const isWorkActiveTask = computed(() =>
  activeTask.value?.completion_type === 'work'
  || activeTask.value?.template?.completion_type === 'work',
)

const isContributionActiveTask = computed(() =>
  activeTask.value?.completion_type === 'contribution'
  || activeTask.value?.template?.completion_type === 'contribution',
)

const isTreasureHuntActiveTask = computed(() =>
  activeTask.value?.completion_type === 'treasure_hunt'
  || activeTask.value?.template?.completion_type === 'treasure_hunt',
)

const isHiddenBossActiveTask = computed(() =>
  activeTask.value?.completion_type === 'hidden_boss'
  || activeTask.value?.template?.completion_type === 'hidden_boss',
)

const buildingHelpRequiredSeconds = computed(() =>
  activeTask.value?.help_required_seconds
  ?? (activeTask.value?.template?.duration_minutes ?? 0) * 60,
)

const buildingHelpProgressSeconds = computed(() =>
  activeTask.value?.help_progress_seconds ?? 0,
)

const buildingHelpRemainingSeconds = computed(() =>
  activeTask.value?.help_remaining_seconds
  ?? Math.max(0, buildingHelpRequiredSeconds.value - buildingHelpProgressSeconds.value),
)

const buildingHelpProgressPercent = computed(() => {
  const total = buildingHelpRequiredSeconds.value
  if (total <= 0) return 100
  return Math.min(100, Math.round((buildingHelpProgressSeconds.value / total) * 100))
})

function formatHelpDuration(totalSeconds: number): string {
  const s = Math.max(0, totalSeconds)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (h > 0) return `${h}小时${m > 0 ? m + '分' : ''}`
  if (m > 0) return `${m}分钟`
  return `${s}秒`
}

const formatBuildingHelpProgress = computed(() => formatHelpDuration(buildingHelpProgressSeconds.value))
const formatBuildingHelpRequired = computed(() => formatHelpDuration(buildingHelpRequiredSeconds.value))

const gatheringRequiredSeconds = computed(() =>
  activeTask.value?.gathering_required_seconds
  ?? (activeTask.value?.template?.duration_minutes ?? 0) * 60,
)

const gatheringProgressSeconds = computed(() =>
  activeTask.value?.gathering_progress_seconds ?? 0,
)

const gatheringRemainingSeconds = computed(() =>
  activeTask.value?.gathering_remaining_seconds
  ?? Math.max(0, gatheringRequiredSeconds.value - gatheringProgressSeconds.value),
)

const gatheringProgressPercent = computed(() => {
  const total = gatheringRequiredSeconds.value
  if (total <= 0) return 100
  return Math.min(100, Math.round((gatheringProgressSeconds.value / total) * 100))
})

const formatGatheringProgress = computed(() => formatHelpDuration(gatheringProgressSeconds.value))
const formatGatheringRequired = computed(() => formatHelpDuration(gatheringRequiredSeconds.value))

const workRequiredSessions = computed(() =>
  activeTask.value?.work_required_sessions
  ?? activeTask.value?.template?.work_required_sessions
  ?? 1,
)

const workProgressSessions = computed(() =>
  activeTask.value?.work_progress_sessions ?? 0,
)

const workRemainingSessions = computed(() =>
  activeTask.value?.work_remaining_sessions
  ?? Math.max(0, workRequiredSessions.value - workProgressSessions.value),
)

const workProgressPercent = computed(() => {
  const total = workRequiredSessions.value
  if (total <= 0) return 100
  return Math.min(100, Math.round((workProgressSessions.value / total) * 100))
})

const contributionRequired = computed(() =>
  activeTask.value?.contribution_required
  ?? activeTask.value?.template?.required_contribution
  ?? activeTask.value?.template?.contribution_required
  ?? 100,
)

const contributionProgress = computed(() =>
  activeTask.value?.contribution_progress ?? 0,
)

const contributionRemaining = computed(() =>
  activeTask.value?.contribution_remaining
  ?? Math.max(0, contributionRequired.value - contributionProgress.value),
)

const contributionProgressPercent = computed(() => {
  const total = contributionRequired.value
  if (total <= 0) return 100
  return Math.min(100, Math.round((contributionProgress.value / total) * 100))
})

const hiddenBossProgressCount = computed(() =>
  activeTask.value?.hidden_boss_progress_count ?? 0,
)

const hiddenBossTotalCount = computed(() =>
  activeTask.value?.hidden_boss_total_count
  ?? activeTask.value?.template?.hidden_boss_total_count
  ?? activeTask.value?.template?.max_completions
  ?? 6,
)

const hiddenBossProgressPercent = computed(() => {
  const total = hiddenBossTotalCount.value
  if (total <= 0) return 100
  return Math.min(100, Math.round((hiddenBossProgressCount.value / total) * 100))
})

const canClaimTaskReward = computed(() => {
  if (isBuildingHelpActiveTask.value) return buildingHelpRemainingSeconds.value <= 0
  if (isGatheringActiveTask.value) return gatheringRemainingSeconds.value <= 0
  if (isWorkActiveTask.value) return workRemainingSessions.value <= 0
  if (isContributionActiveTask.value) return contributionRemaining.value <= 0
  return taskRemainingSeconds.value <= 0
})

// ── 倒计时（timer 类型） ──

const taskRemainingSeconds = computed(() => {
  if (!activeTask.value || !activeTask.value.template) return 0
  const started = activeTask.value.started_at
  if (!started) return 0
  const durationMs = (activeTask.value.template.duration_minutes ?? 0) * 60 * 1000
  const endMs = new Date(started).getTime() + durationMs
  return Math.max(0, Math.floor((endMs - now.value) / 1000))
})

const taskProgressPercent = computed(() => {
  if (!activeTask.value?.template) return 0
  const total = (activeTask.value.template.duration_minutes ?? 1) * 60
  if (total <= 0) return 100
  return Math.min(100, Math.round((1 - taskRemainingSeconds.value / total) * 100))
})

const formatTaskRemaining = computed(() => {
  const s = taskRemainingSeconds.value
  if (s <= 0) return '已完成'
  if (s < 60) return '即将完成'
  const m = Math.floor(s / 60)
  const sec = s % 60
  return `${m}分${sec > 0 ? sec + '秒' : ''}`
})

// ── 工具函数 ──

function statLabel(stat: string): string {
  const map: Record<string, string> = {
    strength: '力量', agility: '敏捷', intelligence: '智力', luck: '运气',
  }
  return map[stat] ?? stat
}

function categoryLabel(cat: string): string {
  const map: Record<string, string> = {
    gathering: '采集', work: '帮工', training: '修炼', exploration: '探索',
  }
  return map[cat] ?? cat
}

function materialIcon(mat: string): string {
  const map: Record<string, string> = { wood: '🪵', stone: '⛏️', herb: '🌿' }
  return map[mat] ?? '📦'
}

type TaskRewardItemEntry = string | RewardPreviewItem

const rewardPreview = ref<RewardPreviewItem | null>(null)

function openRewardPreview(entry: TaskRewardItemEntry) {
  rewardPreview.value = typeof entry === 'string' ? { name: entry } : entry
}

function closeRewardPreview() {
  rewardPreview.value = null
}

function taskRewardPoolItemName(entry: TaskRewardItemEntry): string {
  if (typeof entry === 'string') return entry
  return entry.name ?? entry.item_name ?? '物品'
}

function taskRewardItemLabel(entry: TaskRewardItemEntry): string {
  if (typeof entry === 'string') return entry
  const name = entry.name ?? entry.item_name ?? '物品'
  const qty = entry.quantity ?? 1
  return qty > 1 ? `${name}×${qty}` : name
}

function taskRewardItemRarity(entry: TaskRewardItemEntry): string | undefined {
  if (typeof entry === 'string') return undefined
  return entry.rarity || undefined
}

function buildTaskCompleteMessage(tmpl?: TaskInstance['template']): string {
  if (!tmpl) return '任务完成！'
  if (tmpl.completion_type === 'treasure_hunt') {
    let msg = `「${tmpl.name ?? '机缘门票'}」搜寻完成！`
    if (tmpl.narrative) msg += `\n${tmpl.narrative}`
    return msg
  }
  const rewards = tmpl.rewards
  const parts: string[] = []
  if (rewards?.gold) parts.push(`💰${rewards.gold}`)
  if (rewards?.exp) parts.push(`✨${rewards.exp}`)
  if (rewards?.materials) {
    for (const [mat, amt] of Object.entries(rewards.materials)) {
      parts.push(`${materialIcon(mat)}+${amt}`)
    }
  }
  if (rewards?.stat) {
    for (const [stat, amt] of Object.entries(rewards.stat)) {
      parts.push(`${statLabel(stat)}+${amt}`)
    }
  }
  if (rewards?.items) {
    for (const entry of rewards.items) {
      const label = taskRewardItemLabel(entry)
      const rarity = taskRewardItemRarity(entry)
      parts.push(rarity ? `🎁${label}(${rarityLabel(rarity)})` : `🎁${label}`)
    }
  }
  let msg = parts.length ? `任务完成！ ${parts.join('、')}` : `「${tmpl.name ?? '任务'}」已完成！`
  if (tmpl.narrative) msg += `\n${tmpl.narrative}`
  return msg
}

function adventurerIdsForRefresh(): number[] {
  const ids = new Set<number>()
  const mine = myAdventurer.value
  if (mine) ids.add(mine.id)
  if (activeTask.value?.adventurer_id) ids.add(activeTask.value.adventurer_id)
  return [...ids]
}

function setActiveTask(task: TaskInstance | null, adventurerName = '') {
  if (task?.status === 'accepted') {
    activeTask.value = task
    activeAdvName.value = adventurerName
    return
  }
  activeTask.value = null
  activeAdvName.value = ''
}

async function onTaskSettled(prevTask?: TaskInstance | null) {
  const tmpl = prevTask?.template
  message.success(buildTaskCompleteMessage(tmpl))
  emit('refreshed')
  await Promise.all([
    loadData(),
    store.fetchMyMaterials(),
    store.fetchAdventurers(),
  ])
  void store.refreshTaskAcceptableCount()
}

async function onAdventurerChanged(payload: AdventurerChangedPayload) {
  const hitIds = payload?.ids
  if (!Array.isArray(hitIds) || hitIds.length === 0) return
  const refreshIds = adventurerIdsForRefresh()
  if (!hitIds.some(id => refreshIds.includes(id))) return

  if (payload.action === 'task_complete') {
    const prev = activeTask.value
    const isTreasureHunt = prev?.completion_type === 'treasure_hunt'
      || prev?.template?.completion_type === 'treasure_hunt'
    if (isTreasureHunt) {
      emit('refreshed')
      await Promise.all([
        loadData(),
        store.fetchMyMaterials(),
        store.fetchAdventurers(),
      ])
      void store.refreshTaskAcceptableCount()
      return
    }
    await onTaskSettled(prev)
    return
  }

  if (payload.action === 'treasure_map_found') {
    const dropLabel = payload.dropped_item || '机缘门票'
    message.success(`🎟️ 发现了「${dropLabel}」！`)
    emit('refreshed')
    await loadData()
    return
  }

  if (payload.action === 'task_expired' && payload.reason === 'treasure_hunt_ended') {
    message.info('机缘门票限时掉落已发完，你的搜寻委托已失效')
    emit('refreshed')
    await loadData()
    return
  }

  if (payload.action === 'task_expired' && payload.reason === 'hidden_boss_ended') {
    message.info('隐藏Boss已全部被讨伐，你的讨伐委托已失效')
    emit('refreshed')
    await loadData()
    return
  }

  if (payload.action === 'status_changed' && payload.reason === 'expedition_finished') {
    if (!activeTask.value || !isExpeditionActiveTask.value) return
    const prev = activeTask.value
    await loadData()
    if (!activeTask.value && prev) {
      await onTaskSettled(prev)
    }
  }
}

// ── 操作 ──

// MAX_PER_PLAYER 默认 1：直接取首个空闲冒险者接取，不再弹"选择冒险者"层
async function onTaskClick(task: TaskTemplate) {
  if (!task.can_accept) {
    message.error(task.reject_reason ?? '无法接取')
    return
  }
  if (accepting.value) return
  if (store.adventurers.length === 0) {
    await store.fetchAdventurers()
  }
  const candidate = myAdventurer.value
  if (!candidate) {
    message.error('你还没有冒险者，请先创建')
    return
  }
  if (candidate.status === 'exploring') {
    message.error('探险中无法接取任务')
    return
  }
  if (!confirm(`确定接取「${task.name}」吗？`)) return
  accepting.value = true
  try {
    const res = await store.acceptTask(task.id, candidate.id, props.building.id)
    if (res.code === 200) {
      message.success('任务接取成功！')
      emit('refreshed')
      await loadData()
      void store.refreshTaskAcceptableCount()
    } else {
      message.error(res.message || '接取失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '接取失败')
  } finally {
    accepting.value = false
  }
}

async function abandonTask() {
  if (!activeTask.value) return
  if (activeTask.value.status !== 'accepted') {
    message.warning('当前任务已结束，已刷新任务栏')
    setActiveTask(null)
    await loadData()
    return
  }
  if (!confirm('确定放弃当前任务吗？')) return
  abandoning.value = true
  try {
    const res = await store.abandonTask(activeTask.value.id)
    if (res.code === 200) {
      message.success('已放弃任务')
      emit('refreshed')
      await loadData()
      void store.refreshTaskAcceptableCount()
    } else {
      message.error(res.message || '放弃失败')
      if ((res.message || '').includes('进行中')) {
        setActiveTask(null)
        await loadData()
      }
    }
  } catch (e: any) {
    const errMessage = e?.response?.data?.message || '放弃失败'
    message.error(errMessage)
    if (errMessage.includes('进行中')) {
      setActiveTask(null)
      await loadData()
    }
  } finally {
    abandoning.value = false
  }
}

async function completeTask() {
  if (!activeTask.value) return
  completing.value = true
  try {
    const res = await store.completeTask(activeTask.value.id)
    if (res.code === 200) {
      message.success(buildTaskCompleteMessage(activeTask.value?.template))
      emit('refreshed')
      await Promise.all([loadData(), store.fetchMyMaterials()])
      void store.refreshTaskAcceptableCount()
    } else {
      message.error(res.message || '完成失败')
      if ((res.message || '').includes('状态异常')) {
        setActiveTask(null)
        await loadData()
      }
    }
  } catch (e: any) {
    const errMessage = e?.response?.data?.message || '完成失败'
    message.error(errMessage)
    if (errMessage.includes('状态异常')) {
      setActiveTask(null)
      await loadData()
    }
  } finally {
    completing.value = false
  }
}

// ── 数据加载 ──

async function loadData() {
  loading.value = true
  try {
    // 始终刷新冒险者状态，避免缓存旧 status 导致任务栏误判
    await store.fetchAdventurers()
    const adv = myAdventurer.value

    // 使用合并接口一次性获取任务列表 + 活跃任务
    const boardRes = await store.fetchTaskBoard(adv?.id)
    if (boardRes.code === 200) {
      tasks.value = boardRes.data?.tasks ?? []
      const activeTask = boardRes.data?.active_task
      setActiveTask(activeTask?.id ? activeTask : null, adv?.name)
    } else {
      tasks.value = []
      setActiveTask(null)
    }
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadData()
  socket.on('adventurer_changed', onAdventurerChanged)
})

onUnmounted(() => {
  socket.off('adventurer_changed', onAdventurerChanged)
})

let lastBuildingHelpRefresh = 0
watch(now, async () => {
  if (!isBuildingHelpActiveTask.value || !myAdventurer.value) return
  const t = Date.now()
  if (t - lastBuildingHelpRefresh < 30000) return
  lastBuildingHelpRefresh = t
  const activeRes = await store.fetchActiveTask(myAdventurer.value.id)
  setActiveTask(activeRes.code === 200 && activeRes.data?.id ? activeRes.data : null, activeAdvName.value)
})
</script>

<style scoped>
.task-board-panel {
  margin-bottom: 16px;
}

.task-board-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 8px;
}

.btn-task-admin {
  padding: 4px 10px;
  border-radius: 6px;
  border: 1px solid var(--color-border);
  background: var(--color-surface, var(--color-bg));
  color: var(--color-text);
  font-size: 0.82em;
  cursor: pointer;
}

.btn-task-admin:hover {
  border-color: var(--color-primary);
}

.section-title {
  font-size: 0.9em;
  margin: 0 0 8px;
}

/* ── 任务列表 ── */
.task-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 300px), 1fr));
  gap: 8px;
}
.task-card {
  display: flex;
  min-width: 0;
  flex-direction: column;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 12px;
  cursor: pointer;
  transition: border-color 0.2s;
}
.task-card:hover:not(.disabled) {
  border-color: var(--color-primary);
}
.task-card.disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.task-card.ended {
  opacity: 0.65;
}
.task-card-header {
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr) auto;
  align-items: start;
  gap: 10px;
  margin-bottom: 6px;
}
.task-icon {
  font-size: 1.5em;
}
.task-card-info {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}
.task-name {
  font-weight: 600;
  font-size: 0.95em;
}
.task-category {
  font-size: 0.75em;
  color: var(--color-text-secondary);
}
.task-duration {
  min-width: 0;
  max-width: 100px;
  text-align: right;
  overflow-wrap: anywhere;
  font-size: 0.8em;
  color: var(--color-text-secondary);
}
.task-desc {
  font-size: 0.85em;
  color: var(--color-text-secondary);
  margin-bottom: 8px;
  line-height: 1.4;
}
.task-quota {
  font-size: 0.8em;
  color: var(--color-text-secondary);
  margin-bottom: 8px;
}
.task-expedition-hint,
.task-building-help {
  margin-bottom: 10px;
}
.expedition-note {
  font-size: 0.82em;
  color: var(--color-text-secondary);
  margin: 8px 0 0;
  text-align: center;
}
.task-meta {
  display: grid;
  gap: 6px;
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px solid var(--color-border);
  font-size: 0.8em;
}
.task-requirements {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  color: var(--color-warning);
}
.task-rewards {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  color: var(--color-success);
}
.task-reward-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.task-reward-item-name {
  max-width: 12em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.task-reward-preview-trigger {
  cursor: pointer;
  text-decoration: underline dotted;
  text-underline-offset: 3px;
}
.task-reward-preview-trigger:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
  border-radius: 3px;
}
.task-reward-treasure-line {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}
.task-reward-treasure-summary {
  font-weight: 600;
}
.task-reward-pool {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px;
}
.task-reward-pool-sep {
  color: var(--color-text-muted, var(--color-text));
  opacity: 0.45;
}
.task-rewards .rarity-pill {
  font-size: 0.72em;
  padding: 0 4px;
  border: 1px solid;
  border-radius: 4px;
  line-height: 1.35;
}
.task-reject-reason {
  font-size: 0.8em;
  color: var(--color-danger);
  margin-top: 6px;
}

/* ── 进行中任务 ── */
.active-task-section {
  margin-bottom: 16px;
}
.active-task-card {
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 14px;
}
.task-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.task-adv-name {
  font-size: 0.85em;
  color: var(--color-text-secondary);
}
.task-narrative {
  font-size: 0.9em;
  color: var(--color-text-secondary);
  line-height: 1.6;
  margin-bottom: 12px;
  padding: 10px;
  background: rgba(0, 0, 0, 0.15);
  border-radius: 6px;
  font-style: italic;
}
.task-active-desc {
  font-size: 0.9em;
  color: var(--color-text-secondary);
  line-height: 1.5;
  margin-bottom: 12px;
}
.task-timer {
  margin-bottom: 10px;
}
.task-progress {
  background: var(--color-success);
}
.task-complete-btn {
  display: block;
  width: 100%;
  margin-top: 8px;
}
.task-abandon-btn {
  display: block;
  width: 100%;
  margin-top: 8px;
  background: transparent;
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border);
}
.task-abandon-btn:hover:not(:disabled) {
  color: var(--color-danger);
  border-color: var(--color-danger);
}

.empty-helpers {
  text-align: center;
  color: var(--color-text-secondary);
  font-size: 0.85em;
  padding: 12px;
}

/* ── 共用 ── */
.progress-bar-lg {
  height: 10px;
  background: var(--color-border);
  border-radius: 5px;
  overflow: hidden;
  margin-bottom: 6px;
}
.progress-fill-lg {
  height: 100%;
  background: var(--color-primary);
  border-radius: 5px;
  transition: width 0.3s;
}
.remaining-text {
  font-size: 0.85em;
  color: var(--color-warning);
  text-align: center;
}
.advs1-submit {
  background: var(--color-primary);
  color: #fff;
  border: none;
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
}
.advs1-submit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 驻地任务板：木框、钉角与委托纸张层次。 */
.task-board-panel { --board-brass: color-mix(in srgb, #5b84b2 74%, var(--color-primary)); }
.section-title {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-text);
  letter-spacing: 0.04em;
}
.section-title::before {
  content: '';
  width: 18px;
  height: 2px;
  background: var(--board-brass);
  box-shadow: 0 0 8px color-mix(in srgb, var(--board-brass) 38%, transparent);
}
.task-list { gap: 10px; }
.task-card,
.active-task-card {
  position: relative;
  overflow: hidden;
  border-color: color-mix(in srgb, var(--board-brass) 18%, var(--color-border));
  border-radius: 12px;
  background:
    linear-gradient(100deg, color-mix(in srgb, var(--board-brass) 6%, transparent), transparent 42%),
    var(--color-bg);
  background-color: #0d1117;
  background-color: var(--color-bg, #0d1117);
  box-shadow: inset 0 1px color-mix(in srgb, var(--color-text) 4%, transparent);
}
.task-card::after,
.active-task-card::after {
  content: '';
  position: absolute;
  top: 7px;
  right: 7px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: color-mix(in srgb, var(--board-brass) 55%, var(--color-border));
  box-shadow: inset 0 1px rgba(255, 255, 255, 0.28);
}
.task-card { transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease; }
.task-card:hover:not(.disabled) {
  border-color: var(--board-brass);
  transform: translateX(4px);
  box-shadow: -3px 0 0 var(--board-brass), 0 8px 18px rgba(0, 0, 0, 0.09);
}
.active-task-card {
  border-color: color-mix(in srgb, var(--board-brass) 45%, var(--color-border));
  box-shadow: inset 3px 0 var(--board-brass), 0 8px 22px rgba(0, 0, 0, 0.08);
}
.task-icon { filter: drop-shadow(0 4px 6px color-mix(in srgb, var(--board-brass) 22%, transparent)); }
.progress-fill-lg,
.task-progress {
  background: linear-gradient(90deg, var(--board-brass), color-mix(in srgb, var(--board-brass) 55%, var(--color-primary)));
  background-size: 180% 100%;
  animation: task-progress-flow 3s linear infinite;
}
@keyframes task-progress-flow { to { background-position: -180% 0; } }

@media (prefers-reduced-motion: reduce) {
  .task-card,
  .progress-fill-lg,
  .task-progress { animation: none; transition: none; }
}
@media (max-width: 640px) {
  .task-card-header, .task-header { flex-wrap: wrap; gap: 8px; }
  .task-card-info { min-width: 0; }
  .task-meta { flex-direction: column; align-items: flex-start; gap: 10px; }
  .task-requirements { flex-wrap: wrap; }
  .task-rewards { gap: 8px; }
  .task-reward-preview-trigger { min-height: 32px; padding-block: 4px; }
  .task-reward-item-name { max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
  .task-desc, .task-active-desc, .task-narrative { font-size: 13px; line-height: 1.6; }
  .task-card:hover:not(.disabled) { transform: none; }
  .progress-fill-lg, .task-progress { animation: none; }
}
</style>
