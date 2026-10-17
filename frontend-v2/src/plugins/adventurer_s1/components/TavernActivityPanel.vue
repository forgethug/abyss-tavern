<template>
  <div class="activity-panel">
    <div v-if="loading && !payload.activity" class="activity-empty">活动信息加载中…</div>
    <div v-else-if="!payload.activity" class="activity-empty">
      <strong>🎭 酒馆活动</strong>
      <p>牌桌暂时空着。下一场盛会开始时，酒馆会在这里张榜。</p>
      <button v-if="isAdmin" type="button" class="activity-primary" @click="createDefault">
        创建“赌王大赛”草稿
      </button>
    </div>

    <template v-else>
      <header class="activity-hero">
        <div>
          <span class="activity-kicker">TAVERN STORY EVENT</span>
          <h3>{{ payload.activity.name }}</h3>
          <p>{{ payload.activity.description || '同桌斗牌，读局、博弈，直到排出最后名次。' }}</p>
        </div>
        <span class="activity-status">{{ activityStatus(payload.activity.status) }}</span>
      </header>

      <div class="activity-rules">
        <span v-if="payload.activity.gameplay">
          {{ payload.activity.gameplay.display_name }}
        </span>
        <span>每组至多 {{ payload.activity.rules.group_size }} 人</span>
        <span>每组前 {{ payload.activity.rules.advance_count }} 晋级</span>
        <span>签到窗 {{ payload.activity.rules.checkin_minutes }} 分钟</span>
        <span>每场消耗 {{ payload.activity.rules.stamina_cost }} 精力</span>
        <span>
          初始 {{ payload.activity.rules.poker_starting_stack }} 筹码
          · 盲注 {{ payload.activity.rules.poker_small_blind }}/{{ payload.activity.rules.poker_big_blind }}
        </span>
        <span>每 {{ payload.activity.rules.poker_blind_raise_every_hands }} 手牌盲注翻倍</span>
        <span>每天进行 1 个阶段</span>
      </div>
      <div v-if="payload.activity.gameplay" class="activity-gameplay">
        <strong>{{ payload.activity.gameplay.display_name }}</strong>
        <p v-if="payload.activity.gameplay.rules_summary">
          {{ payload.activity.gameplay.rules_summary }}
        </p>
        <ol v-if="payload.activity.gameplay.table_rules?.length">
          <li v-for="rule in payload.activity.gameplay.table_rules" :key="rule">{{ rule }}</li>
        </ol>
        <template v-else>
          <p>每轮：{{ payload.activity.gameplay.round_cycle?.join(' → ') }}</p>
          <p>排名依据：{{ payload.activity.gameplay.ranking_dimensions?.join('、') }}</p>
        </template>
        <p v-if="payload.activity.gameplay.ranking_rule">
          <b>如何排名：</b>{{ payload.activity.gameplay.ranking_rule }}
        </p>
        <small v-if="payload.activity.gameplay.asset_boundary">
          {{ payload.activity.gameplay.asset_boundary }}
        </small>
      </div>

      <div v-if="payload.activity.status === 'registration'" class="registration-card">
        <div>
          <strong>全赛报名开放中</strong>
          <p>已报名 {{ payload.registration_count }} 人。抽签后对手固定，签到顺序不会改变分组。</p>
        </div>
        <button
          v-if="!payload.my_registration"
          type="button"
          class="activity-primary"
          :disabled="acting || !activeAdventurer"
          @click="register"
        >报名参赛</button>
        <button
          v-else
          type="button"
          class="activity-secondary"
          :disabled="acting"
          @click="withdraw"
        >取消报名</button>
      </div>

      <div v-if="myCurrentMatch" class="my-match">
        <div>
          <span class="activity-kicker">MY ASSIGNED GROUP</span>
          <h4>第 {{ myCurrentMatch.stage_number }} 阶段 · {{ myCurrentMatch.group_number }} 号组</h4>
          <p>签到开放：{{ formatDateTime(myCurrentMatch.checkin_open_at) }}</p>
          <p>窗口内处于休息状态且精力足够时，系统会自动完成签到。</p>
          <p>开赛时间：{{ formatDateTime(myCurrentMatch.starts_at) }}</p>
        </div>
        <button
          v-if="canCheckIn(myCurrentMatch)"
          type="button"
          class="activity-primary"
          :disabled="acting"
          @click="checkIn(myCurrentMatch)"
        >立即签到（也会自动）</button>
        <span v-else class="my-match-state">{{ myMatchState(myCurrentMatch) }}</span>
      </div>

      <section v-if="payload.stages.length" class="activity-stage-browser">
        <div class="stage-tabs" role="tablist" aria-label="比赛阶段">
          <button
            v-for="stage in payload.stages"
            :key="stage.stage_number"
            type="button"
            class="stage-tab"
            :class="{ active: stage.stage_number === selectedStage }"
            :disabled="loading"
            @click="selectStage(stage.stage_number)"
          >
            第 {{ stage.stage_number }} 阶段
            <small>{{ stage.match_count }} 场</small>
          </button>
        </div>

        <div class="stage-section">
        <div class="section-heading">
          <h4>第 {{ selectedStage }} 阶段分组</h4>
          <span v-if="selectedStage === payload.activity.current_stage">
            各组错峰开赛；休息且精力足够会自动签到，截止时仍未签到将判负
          </span>
          <span v-else>本阶段已揭晓比赛记录</span>
        </div>
        <div class="match-grid">
          <article v-for="match in payload.matches" :key="match.id" class="match-card">
            <div class="match-head">
              <strong>{{ match.is_final ? '决赛' : `${match.group_number} 号组` }}</strong>
              <span>{{ matchStatus(match.status) }}</span>
            </div>
            <p class="match-time">{{ formatDateTime(match.starts_at) }} 开赛</p>
            <ol class="participant-list">
              <li v-for="participant in match.participants" :key="participant.adventurer_id">
                <span :class="{ mine: participant.is_mine }">{{ participant.adventurer_name }}</span>
                <small v-if="participant.status === 'forfeit'" class="forfeit">
                  弃权<span v-if="participant.forfeit_reason">：{{ participant.forfeit_reason }}</span>
                </small>
                <small v-else-if="participant.rank">第 {{ participant.rank }} 名</small>
                <small v-else-if="participant.checked_in">已签到</small>
                <small v-else>待签到</small>
              </li>
            </ol>
            <template v-if="match.story">
              <button
                type="button"
                class="story-toggle"
                :aria-expanded="isStoryExpanded(match.id)"
                @click="toggleStory(match.id)"
              >
                {{ isStoryExpanded(match.id) ? '收起牌局' : '查看牌局' }}
              </button>
              <div v-if="isStoryExpanded(match.id)" class="match-story">{{ match.story }}</div>
            </template>
            <p
              v-else-if="match.status === 'failed'"
              class="story-pending"
            >
              命运牌桌忽然停转，这场比赛出了些意外。酒馆已经收好筹码与座次，
              待牌桌重新亮起后再次开局。
            </p>
            <p
              v-else-if="match.story_status === 'failed'"
              class="story-pending"
            >
              <template v-if="match.status === 'settled'">
                这一桌的记录在喧闹中出了些意外。名次已经刻上榜单，待记录水晶重新亮起，
                这场牌局的故事会再次展开。
              </template>
              <template v-else>
                这一桌的记录在喧闹中出了些意外。待记录水晶重新亮起，
                这场牌局的故事会再次展开。
              </template>
            </p>
            <p
              v-else-if="match.status === 'settled' && match.story_status !== 'complete'"
              class="story-pending"
            >
              赛果已经由命运牌桌裁定，酒馆记录人仍在整理这一桌的故事。
            </p>
            <button
              v-if="isAdmin && match.story_status === 'failed'"
              type="button"
              class="activity-secondary"
              :disabled="acting"
              @click="retryMatch(match.id)"
            >重新唤醒记录水晶</button>
          </article>
        </div>
        <div v-if="!payload.matches.length" class="activity-empty stage-empty">
          本阶段暂无比赛记录。
        </div>
        <nav
          v-if="payload.match_pagination.total_pages > 1"
          class="match-pagination"
          aria-label="比赛日志分页"
        >
          <button
            type="button"
            class="activity-secondary"
            :disabled="loading || payload.match_pagination.page <= 1"
            @click="changePage(payload.match_pagination.page - 1)"
          >上一页</button>
          <span>
            第 {{ payload.match_pagination.page }} / {{ payload.match_pagination.total_pages }} 页
            · 共 {{ payload.match_pagination.total }} 场
          </span>
          <button
            type="button"
            class="activity-secondary"
            :disabled="loading || payload.match_pagination.page >= payload.match_pagination.total_pages"
            @click="changePage(payload.match_pagination.page + 1)"
          >下一页</button>
        </nav>
        </div>
      </section>

      <section v-if="payload.final_ranking.length" class="final-ranking">
        <h4>最终排名</h4>
        <div v-for="row in payload.final_ranking" :key="row.id" class="ranking-row">
          <b>#{{ row.final_rank }}</b><span>{{ row.adventurer_name }}</span>
        </div>
      </section>

      <section v-if="isAdmin" class="activity-admin">
        <h4>活动管理</h4>
        <label>
          首组开赛时间
          <input
            v-model="firstStageLocal"
            type="datetime-local"
            :min="firstStageMinLocal"
          >
          <small class="activity-field-hint">
            刷新后会自动填入满足签到窗口的最近可用时间，也可以手动改晚。
          </small>
        </label>
        <div class="admin-actions">
          <button
            v-if="['completed', 'cancelled'].includes(payload.activity.status)"
            type="button"
            class="activity-primary"
            :disabled="acting"
            @click="createDefault"
          >创建下一期“赌王大赛”</button>
          <button
            v-if="payload.activity.status === 'draft'"
            type="button"
            class="activity-primary"
            :disabled="acting"
            @click="openRegistration"
          >开放报名</button>
          <button
            v-if="payload.activity.status === 'registration'"
            type="button"
            class="activity-primary"
            :disabled="acting || !firstStageLocal"
            @click="draw"
          >截止报名并抽签</button>
          <button
            v-if="['draft', 'registration', 'running'].includes(payload.activity.status)"
            type="button"
            class="activity-danger"
            :disabled="acting"
            @click="cancelActivity"
          >取消活动</button>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import api from '@/composables/useApi'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { useAdminSecondaryAccess } from '@/composables/useAdminSecondaryAccess'
import { formatDateTime } from '@/utils/time'
import { useAdventurerS1Store } from '../stores/adventurerS1'

interface Participant {
  adventurer_id: number
  adventurer_name: string
  checked_in: boolean
  is_mine: boolean
  status: string
  forfeit_reason: string | null
  rank: number | null
}
interface ActivityMatch {
  id: number
  stage_number: number
  group_number: number
  is_final: boolean
  status: string
  checkin_open_at: string
  starts_at: string
  reveal_at: string
  story: string | null
  story_status: string | null
  participants: Participant[]
}
interface ActivityPayload {
  activity: null | {
    id: number
    name: string
    description: string
    status: string
    current_stage: number
    first_stage_start_at: string | null
    rules: Record<string, number>
    gameplay: null | {
      display_name: string
      rules_summary?: string
      table_rules?: string[]
      ranking_rule?: string
      asset_boundary?: string
      round_cycle?: string[]
      ranking_dimensions?: string[]
    }
  }
  registration_count: number
  my_registration: null | {
    id: number
    adventurer_id: number
    adventurer_name: string
    status: string
    final_rank: number | null
  }
  my_current_match: ActivityMatch | null
  stages: Array<{
    stage_number: number
    match_count: number
  }>
  matches: ActivityMatch[]
  match_pagination: {
    stage_number: number
    page: number
    per_page: number
    total: number
    total_pages: number
  }
  final_ranking: Array<{
    id: number
    adventurer_name: string
    final_rank: number
  }>
}

const emptyPayload = (): ActivityPayload => ({
  activity: null,
  registration_count: 0,
  my_registration: null,
  my_current_match: null,
  stages: [],
  matches: [],
  match_pagination: {
    stage_number: 1,
    page: 1,
    per_page: 6,
    total: 0,
    total_pages: 1,
  },
  final_ranking: [],
})
const payload = ref<ActivityPayload>(emptyPayload())
const loading = ref(false)
const acting = ref(false)
const firstStageLocal = ref('')
const firstStageMinLocal = ref('')
const firstStageActivityId = ref<number | null>(null)
const selectedStage = ref<number | null>(null)
const selectedPage = ref(1)
const expandedStories = ref<Record<number, boolean>>({})
const store = useAdventurerS1Store()
const message = useGlobalMessage()
const { canUseFullAdminUi: isAdmin } = useAdminSecondaryAccess()
const activeAdventurer = computed(() => store.adventurers.find(
  adventurer => !['dead', 'archived'].includes(adventurer.status),
) ?? null)
const myCurrentMatch = computed(() => payload.value.my_current_match)
let refreshTimer: ReturnType<typeof setInterval> | null = null

async function load(options: { resetStage?: boolean } = {}) {
  if (loading.value) return
  loading.value = true
  try {
    const params: Record<string, number> = {
      page: options.resetStage ? 1 : selectedPage.value,
    }
    if (!options.resetStage && selectedStage.value !== null) {
      params.stage = selectedStage.value
    }
    const { data } = await api.get('/adventurer-s1/activities/current', {
      params,
      s1RealmContext: 'membership',
    })
    if (data.code === 200) {
      payload.value = { ...emptyPayload(), ...data.data }
      selectedStage.value = payload.value.match_pagination.stage_number
      selectedPage.value = payload.value.match_pagination.page
      if (options.resetStage) expandedStories.value = {}
      ensureFirstStageTime()
    }
  } finally {
    loading.value = false
  }
}

async function selectStage(stageNumber: number) {
  if (stageNumber === selectedStage.value || loading.value) return
  selectedStage.value = stageNumber
  selectedPage.value = 1
  expandedStories.value = {}
  await load()
}

async function changePage(page: number) {
  if (page === selectedPage.value || loading.value) return
  selectedPage.value = page
  expandedStories.value = {}
  await load()
}

function isStoryExpanded(matchId: number): boolean {
  return !!expandedStories.value[matchId]
}

function toggleStory(matchId: number) {
  expandedStories.value = {
    ...expandedStories.value,
    [matchId]: !expandedStories.value[matchId],
  }
}

function toDatetimeLocalValue(value: Date): string {
  const localTime = new Date(value.getTime() - value.getTimezoneOffset() * 60_000)
  return localTime.toISOString().slice(0, 16)
}

function ensureFirstStageTime() {
  const activity = payload.value.activity
  if (!activity || activity.status !== 'registration') return

  const checkinMinutes = Number(activity.rules.checkin_minutes) || 60
  // 后端要求严格晚于完整签到窗口；额外留 5 分钟，避免分钟截断和请求延迟造成边界失败。
  const earliest = new Date(Date.now() + (checkinMinutes + 5) * 60_000)
  firstStageMinLocal.value = toDatetimeLocalValue(earliest)

  if (firstStageActivityId.value === activity.id && firstStageLocal.value) {
    const selectedStart = new Date(firstStageLocal.value)
    if (Number.isFinite(selectedStart.getTime()) && selectedStart > earliest) return
  }

  const persistedStart = activity.first_stage_start_at
    ? new Date(activity.first_stage_start_at)
    : null
  const suggestedStart = persistedStart
    && Number.isFinite(persistedStart.getTime())
    && persistedStart > earliest
    ? persistedStart
    : earliest
  firstStageLocal.value = toDatetimeLocalValue(suggestedStart)
  firstStageActivityId.value = activity.id
}

async function mutate(action: () => Promise<any>, successText?: string) {
  acting.value = true
  try {
    const response = await action()
    const body = response.data
    if (body.code !== 200 && body.code !== 201) {
      message.error(body.message || '操作失败')
      return
    }
    message.success(body.message || successText || '操作成功')
    await Promise.all([load({ resetStage: true }), store.fetchAdventurers(true)])
  } catch (error: any) {
    message.error(error?.response?.data?.message || '操作失败')
  } finally {
    acting.value = false
  }
}

function createDefault() {
  void mutate(() => api.post('/adventurer-s1/admin/activities', {
    name: '赌王大赛',
    activity_type: 'narrative_tournament',
    description: '命运牌桌已经开席：牌桌裁定每一张牌与每一枚筹码，酒馆记录人传唱其中的抉择与逆转。',
  }, { s1RealmContext: 'membership' }))
}
function openRegistration() {
  void mutate(() => api.post(
    `/adventurer-s1/admin/activities/${payload.value.activity!.id}/open-registration`,
    {},
    { s1RealmContext: 'membership' },
  ))
}
function draw() {
  void mutate(() => api.post(
    `/adventurer-s1/admin/activities/${payload.value.activity!.id}/draw`,
    { first_stage_start_at: new Date(firstStageLocal.value).toISOString() },
    { s1RealmContext: 'membership' },
  ))
}
function cancelActivity() {
  if (!window.confirm('确定取消本次活动并释放已签到冒险者吗？')) return
  void mutate(() => api.post(
    `/adventurer-s1/admin/activities/${payload.value.activity!.id}/cancel`,
    {},
    { s1RealmContext: 'membership' },
  ))
}
function register() {
  const activity = payload.value.activity
  const adventurer = activeAdventurer.value
  if (!activity || !adventurer) return
  void mutate(() => api.post(
    `/adventurer-s1/activities/${activity.id}/register`,
    { adventurer_id: adventurer.id },
    { s1RealmContext: 'membership' },
  ))
}
function withdraw() {
  const activity = payload.value.activity
  const registration = payload.value.my_registration
  if (!activity || !registration) return
  void mutate(() => api.delete(
    `/adventurer-s1/activities/${activity.id}/register`,
    {
      data: { adventurer_id: registration.adventurer_id },
      s1RealmContext: 'membership',
    },
  ))
}
function checkIn(match: ActivityMatch) {
  const registration = payload.value.my_registration
  if (!registration) return
  void mutate(() => api.post(
    `/adventurer-s1/activity-matches/${match.id}/check-in`,
    { adventurer_id: registration.adventurer_id },
    { s1RealmContext: 'membership' },
  ))
}
function retryMatch(matchId: number) {
  void mutate(() => api.post(
    `/adventurer-s1/admin/activity-matches/${matchId}/retry`,
    {},
    { s1RealmContext: 'membership' },
  ))
}
function canCheckIn(match: ActivityMatch): boolean {
  const mine = match.participants.find(participant => participant.is_mine)
  const now = Date.now()
  return !!mine
    && !mine.checked_in
    && ['scheduled', 'checkin_open'].includes(match.status)
    && now >= new Date(match.checkin_open_at).getTime()
    && now < new Date(match.starts_at).getTime()
}
function myMatchState(match: ActivityMatch): string {
  const mine = match.participants.find(participant => participant.is_mine)
  if (mine?.checked_in && !mine.rank) return '已进入编号组，等待开赛'
  if (mine?.rank) return `本组第 ${mine.rank} 名`
  if (Date.now() < new Date(match.checkin_open_at).getTime()) return '签到尚未开放'
  if (Date.now() >= new Date(match.starts_at).getTime()) return '签到已截止'
  return '等待签到'
}
function activityStatus(status: string): string {
  return ({
    draft: '草稿',
    registration: '报名中',
    running: '进行中',
    completed: '已结束',
    cancelled: '已取消',
  } as Record<string, string>)[status] || status
}
function matchStatus(status: string): string {
  return ({
    scheduled: '等待签到',
    checkin_open: '签到开放',
    quota_wait: '等待剧情额度',
    generating: '剧情生成中',
    awaiting_reveal: '等待揭晓',
    settled: '已揭晓',
    failed: '牌桌意外停转',
    cancelled: '已取消',
  } as Record<string, string>)[status] || status
}

onMounted(async () => {
  await Promise.all([load(), store.fetchAdventurers()])
  // 后端活动调度粒度为 1 分钟；无需用更短轮询放大 200+ 参赛者的读压力。
  refreshTimer = setInterval(load, 60_000)
})
onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<style scoped>
.activity-panel { display: grid; gap: 18px; color: #e8dcc4; }
.activity-empty, .registration-card, .my-match, .activity-admin, .final-ranking {
  padding: 18px; border: 1px solid rgba(210, 170, 100, .28); border-radius: 12px;
  background: rgba(25, 20, 18, .72);
}
.activity-hero { display: flex; justify-content: space-between; gap: 20px; padding: 22px;
  border-radius: 14px; border: 1px solid rgba(214, 161, 69, .4);
  background: linear-gradient(135deg, rgba(70, 35, 24, .9), rgba(26, 22, 28, .92)); }
.activity-hero h3 { margin: 4px 0 8px; font-size: 28px; }
.activity-kicker { color: #d9a653; font-size: 11px; letter-spacing: .16em; }
.activity-status { height: fit-content; padding: 5px 10px; border-radius: 999px;
  background: rgba(217, 166, 83, .14); color: #f0c77d; }
.activity-rules { display: flex; flex-wrap: wrap; gap: 8px; }
.activity-rules span { padding: 6px 10px; border-radius: 999px; background: rgba(255,255,255,.06); }
.activity-gameplay { padding: 14px 16px; border-left: 3px solid #d9a653;
  border-radius: 8px; background: rgba(217, 166, 83, .08); }
.activity-gameplay p { margin: 6px 0 0; color: #c9bda8; line-height: 1.55; }
.activity-gameplay ol { margin: 10px 0; padding-left: 22px; color: #d8cbb5; }
.activity-gameplay li { margin: 6px 0; line-height: 1.55; }
.activity-gameplay small { display: block; color: #a99a86; line-height: 1.5; }
.registration-card, .my-match { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.activity-primary, .activity-secondary, .activity-danger { border: 0; border-radius: 8px;
  padding: 9px 14px; cursor: pointer; }
.activity-primary { color: #25170a; background: #e0ad58; }
.activity-secondary { color: #ead8b8; background: rgba(255,255,255,.1); }
.activity-danger { color: #ffd5d0; background: rgba(170, 54, 44, .5); }
button:disabled { opacity: .55; cursor: not-allowed; }
.activity-stage-browser { display: grid; gap: 14px; }
.stage-tabs { display: flex; flex-wrap: wrap; gap: 8px; }
.stage-tab { display: inline-flex; align-items: center; gap: 7px; padding: 8px 12px;
  border: 1px solid rgba(217, 166, 83, .25); border-radius: 999px; color: #d8cbb5;
  background: rgba(255,255,255,.05); cursor: pointer; }
.stage-tab small { color: #a99a86; }
.stage-tab.active { border-color: rgba(224, 173, 88, .75); color: #25170a;
  background: #e0ad58; }
.stage-tab.active small { color: rgba(37, 23, 10, .7); }
.stage-section { display: grid; gap: 12px; }
.section-heading, .match-head, .ranking-row { display: flex; justify-content: space-between; gap: 12px; }
.match-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
.match-card { padding: 15px; border-radius: 10px; border: 1px solid rgba(255,255,255,.1);
  background: rgba(18, 16, 20, .75); }
.match-time { color: #a99a86; font-size: 13px; }
.participant-list { padding-left: 22px; }
.participant-list li { display: flex; justify-content: space-between; margin: 5px 0; }
.participant-list .mine { color: #f4c66f; font-weight: 700; }
.participant-list small { color: #a99a86; }
.participant-list small.forfeit { color: #dc8f86; text-align: right; }
.story-toggle { width: 100%; margin-top: 10px; padding: 8px 10px; border: 0;
  border-radius: 7px; color: #e8c98f; background: rgba(217, 166, 83, .1); cursor: pointer; }
.match-story { margin-top: 12px; padding-top: 12px; border-top: 1px solid rgba(255,255,255,.08);
  line-height: 1.75; white-space: pre-wrap; }
.story-pending { margin: 12px 0 0; color: #a99a86; line-height: 1.6; }
.stage-empty { padding: 14px; }
.match-pagination { display: flex; align-items: center; justify-content: center;
  gap: 12px; color: #bcae98; }
.ranking-row { padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,.07); }
.activity-admin label { display: grid; gap: 6px; max-width: 320px; }
.activity-admin input { color: #eee; background: #171419; border: 1px solid #554638;
  border-radius: 7px; padding: 8px; }
.activity-field-hint { color: #a99a86; line-height: 1.45; }
.admin-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
@media (max-width: 700px) {
  .activity-hero, .registration-card, .my-match { align-items: stretch; flex-direction: column; }
}
</style>
