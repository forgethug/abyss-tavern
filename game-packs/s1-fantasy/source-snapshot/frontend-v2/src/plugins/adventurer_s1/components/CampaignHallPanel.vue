<template>
  <div class="campaign-hall">
    <header class="hall-masthead">
      <div class="hall-emblem" aria-hidden="true"><Compass :width="30" :height="30" :stroke-width="1.4" /></div>
      <div class="hall-intro">
        <p class="hall-eyebrow">深渊酒馆 · 共同战役</p>
        <h3>战役议事厅</h3>
        <p class="hall-subtitle">各地的发现汇聚于此，下一段故事由你们共同书写。</p>
      </div>
      <button type="button" class="refresh-button" :disabled="initializing || loading" @click="loadBoard(page)">
        <RefreshCw :width="15" :height="15" :class="{ spinning: loading }" aria-hidden="true" /><span>刷新进展</span>
      </button>
      <Compass class="masthead-watermark" :width="180" :height="180" :stroke-width="0.6" aria-hidden="true" />
    </header>
    <p v-if="errorText" class="hall-notice notice-error" role="alert"><Info :width="18" :height="18" aria-hidden="true" /><span>{{ errorText }}</span></p>
    <p v-if="taskCoolingDown" class="hall-notice notice-warning" role="status">
      <Clock3 :width="18" :height="18" aria-hidden="true" />
      <span>活动任务接取冷却中，将于 {{ endedDate(store.campaignTaskCooldownUntil!) }} 恢复。普通探险仍可参加。</span>
    </p>
    <section v-if="(initializing || loading) && !items.length" class="hall-empty hall-loading" role="status" aria-label="正在整理各地消息">
      <div class="loading-lines" aria-hidden="true"><span /><span /><span /></div><p>正在整理各地消息…</p>
    </section>
    <section v-else-if="!items.length && !errorText" class="hall-empty">
      <Compass :width="40" :height="40" :stroke-width="1.2" aria-hidden="true" />
      <h4>{{ hallBuilt ? page > 1 ? '此页暂无事件' : '静候远方的消息' : '议事厅尚未开放' }}</h4>
      <p v-if="hallBuilt && page > 1">各地事件已有更新，可以返回上一页查看。</p>
      <p v-else-if="hallBuilt">尚未收到各地的发现。冒险者在探索途中发现新的事件后，消息会送到这里。</p>
      <p v-else>待议事厅开放，各地的发现与共同的行动将在这里汇集。</p>
    </section>
    <nav v-if="!items.length && page > 1" class="hall-pagination" aria-label="事件分页">
      <button type="button" :disabled="loading" @click="loadBoard(page - 1)"><ChevronLeft :width="14" :height="14" aria-hidden="true" />上一页</button><span>第 {{ page }} 页</span>
    </nav>
    <div v-if="items.length" class="hall-board" :aria-busy="loading">
      <aside class="campaign-directory" aria-label="战役目录">
        <div class="directory-heading"><span><ScrollText :width="16" :height="16" aria-hidden="true" />各地事件</span><span class="hall-muted">本页 {{ items.length }} 项</span></div>
        <nav class="campaign-list" aria-label="选择战役">
          <button v-for="item in items" :key="item.id" type="button" class="campaign-choice"
            :class="{ selected: selectedCampaignId === item.id, archived: item.status === 'ended' }"
            :aria-label="`查看战役：${item.title}`" :aria-pressed="selectedCampaignId === item.id"
            aria-controls="campaign-detail" :disabled="accepting !== null" @click="selectCampaign(item.id)">
            <span class="choice-topline"><span class="choice-status" :class="`tone-${campaignState(item)}`"><span class="status-dot" />{{ directoryStatus[campaignState(item)] }}</span><ChevronRight :width="15" :height="15" class="choice-arrow" aria-hidden="true" /></span>
            <strong class="choice-title">{{ item.title }}</strong>
            <span class="choice-summary">{{ item.summary }}</span>
            <span class="choice-location"><Archive v-if="item.status === 'ended'" :width="13" :height="13" aria-hidden="true" /><MapPin v-else :width="13" :height="13" aria-hidden="true" />{{ item.status === 'ended' ? '战役已归档 · 可回顾战报' : item.target_dungeons.join('、') || '等待后续行动' }}</span>
          </button>
        </nav>
        <nav v-if="page > 1 || hasNext" class="hall-pagination directory-pagination" aria-label="事件分页">
          <button type="button" :disabled="loading || accepting !== null || page === 1" @click="loadBoard(page - 1)"><ChevronLeft :width="14" :height="14" aria-hidden="true" />上一页</button><span>第 {{ page }} 页</span>
          <button type="button" :disabled="loading || accepting !== null || !hasNext" @click="loadBoard(page + 1)">下一页<ChevronRight :width="14" :height="14" aria-hidden="true" /></button>
        </nav>
      </aside>
      <article v-if="selectedCampaign" id="campaign-detail" class="campaign-card campaign-detail" aria-label="战役详情">
        <header class="campaign-heading">
          <div class="campaign-kicker"><span><Flag :width="14" :height="14" aria-hidden="true" />{{ selectedCampaign.isolated_world ? '活动新境' : '共同战役' }}</span><span class="hall-status" :class="`tone-${campaignState(selectedCampaign)}`"><span class="status-dot" />{{ campaignStatus(selectedCampaign) }}</span></div>
          <h4>{{ selectedCampaign.title }}</h4>
          <p v-if="selectedCampaign.isolated_world" class="hall-muted">在这片新天地从初次探索开始。这里的发现与变化由参与活动的小队共同留下。</p>
          <p v-if="selectedCampaign.status !== 'ended'" class="team-capacity"><Users :width="15" :height="15" aria-hidden="true" /><span><b>{{ selectedCampaign.active_team_count || 0 }}</b> / {{ selectedCampaign.max_active_teams || 1 }} 队正在推进关键行动 · 同图可多队探索</span></p>
        </header>
        <section v-if="selectedCampaign.status === 'ended'" class="campaign-ending" aria-label="战役结局">
          <div class="section-heading"><h5><Archive :width="17" :height="17" aria-hidden="true" />战役结局</h5><time v-if="selectedCampaign.ended_at" class="hall-muted" :datetime="selectedCampaign.ended_at">{{ endedDate(selectedCampaign.ended_at) }} · 已归档</time></div>
          <p class="report-text">{{ selectedCampaign.summary }}</p>
        </section>
        <section v-else class="campaign-brief" aria-label="当前局势"><h5 class="section-caption">当前局势</h5><p class="report-text">{{ selectedCampaign.summary }}</p></section>
        <section v-if="selectedCampaign.world_notice" class="campaign-brief" aria-label="战役动向">
          <div class="section-heading"><h5>{{ selectedCampaign.world_notice.title }}</h5><time class="hall-muted" :datetime="selectedCampaign.world_notice.occurred_at">{{ endedDate(selectedCampaign.world_notice.occurred_at) }}</time></div>
          <p class="report-text">{{ selectedCampaign.world_notice.summary }}</p>
        </section>
        <section v-if="selectedCampaign.status !== 'ended' && selectedCampaign.objective && selectedCampaign.target_dungeons.length" class="current-action" aria-label="当前行动">
          <div class="section-heading"><h5><Flag :width="17" :height="17" aria-hidden="true" />当前行动</h5><span class="action-caption">选择现场 · 集结小队</span></div>
          <p class="action-objective report-text">{{ selectedObjective }}</p>
          <p class="action-maps"><MapPin :width="14" :height="14" aria-hidden="true" /><span>相关地图：{{ selectedCampaign.target_dungeons.join('、') }}</span></p>
          <div class="hall-actions">
            <label class="map-field">前往
              <select v-model="selectedMaps[selectedCampaign.id]" :disabled="loading || accepting !== null || !selectedAvailableMaps.length">
                <option value="">{{ selectedAvailableMaps.length ? '选择地图' : '暂无可接取的现场' }}</option>
                <option v-for="map in selectedAvailableMaps" :key="map" :value="map">{{ map }}{{ selectedCampaign.dungeon_levels?.[map] ? ` · Lv.${selectedCampaign.dungeon_levels[map].min_level}-${selectedCampaign.dungeon_levels[map].max_level}` : '' }}{{ selectedCampaign.isolated_world && !selectedCampaign.core_task_dungeons?.includes(map) ? ' · 自由探索' : '' }}</option>
              </select>
            </label>
            <button type="button" class="accept-button" :disabled="loading || !hallBuilt || taskCoolingDown || accepting !== null || !selectedMaps[selectedCampaign.id] || !selectedAvailableMaps.length" @click="accept(selectedCampaign)"><Users :width="17" :height="17" aria-hidden="true" />{{ accepting === selectedCampaign.id ? '组队中…' : '接取并组队' }}<ChevronRight :width="16" :height="16" aria-hidden="true" /></button>
          </div>
          <p v-if="!selectedAvailableMaps.length" class="action-note">当前没有可接取的现场，请留意战报与后续行动。</p>
          <p v-else class="action-note">接取后前往小队页招募并出发。同一地图可容纳多支小队探险，关键线索与行动不保证每次遇到；已有队伍推进时，其他小队仍可正常探索、战斗并获得战利品。</p>
          <p class="hall-muted">每个现场同时由一支小队推进关键行动。报名后入口已更新或关闭时，本次出发取消并解散；取消报名也会解散小队。</p>
          <p v-if="store.campaignTaskPolicy" class="departure-policy"><Clock3 :width="15" :height="15" aria-hidden="true" /><span>接取后 {{ store.campaignTaskPolicy.departure_timeout_minutes }} 分钟未出发，小队自动解散并恢复休息；队员随后 {{ store.campaignTaskPolicy.timeout_cooldown_minutes }} 分钟内不能接取或加入活动任务。</span></p>
        </section>
        <section v-if="selectedCampaign.stage && selectedCampaign.status !== 'ended'" class="stage-section" aria-label="阶段进展">
          <div class="section-heading"><div><h5><Compass :width="17" :height="17" aria-hidden="true" />阶段进展</h5><p class="stage-title">{{ selectedCampaign.stage.title }}</p></div><span v-if="selectedCampaign.stage.nodes.length" class="stage-count">已确认 <b>{{ confirmedNodeCount }}</b> / {{ selectedCampaign.stage.nodes.length }} 处公开现场</span></div>
          <div v-if="selectedCampaign.stage.nodes.length" class="stage-progress" role="progressbar" aria-label="已公开现场确认数量" :aria-valuenow="confirmedNodeCount" :aria-valuemin="0" :aria-valuemax="selectedCampaign.stage.nodes.length"><span :style="{ width: `${confirmedNodeCount / selectedCampaign.stage.nodes.length * 100}%` }" /></div>
          <div class="stage-nodes">
            <div v-for="(node, index) in selectedCampaign.stage.nodes" :key="node.dungeon" class="stage-node" :class="`node-${node.result}`">
              <span class="node-marker" aria-hidden="true"><Check v-if="node.result === 'confirmed'" :width="15" :height="15" /><X v-else-if="node.result === 'failed'" :width="15" :height="15" /><span v-else>{{ index + 1 }}</span></span>
              <div class="node-body">
                <div class="node-heading"><span class="node-dungeon">{{ node.dungeon }}</span><span class="node-result">{{ nodeResultLabel[node.result] }}</span></div>
                <h6>{{ node.title }}</h6><span v-if="node.required_for_success === false" class="optional-label">可选行动</span>
                <p class="node-objective report-text">{{ node.objective }}</p>
                <p v-if="node.challenge" class="node-challenge"><span>建议 Lv.{{ node.challenge.recommended_level }}</span> · {{ node.challenge.brief }}</p>
                <button v-if="node.evidence_log_id" type="button" class="source-log-button" :aria-label="`${node.dungeon} · ${node.title}，查看来源探险日志 ${node.evidence_log_id}`" @click="emit('open-log', node.evidence_log_id)"><BookOpen :width="14" :height="14" aria-hidden="true" />来源：探险日志 #{{ node.evidence_log_id }}<ArrowUpRight :width="13" :height="13" aria-hidden="true" /></button>
              </div>
            </div>
          </div>
          <p v-if="selectedCampaign.stage.status === 'ready'" class="stage-ready"><Clock3 :width="16" :height="16" aria-hidden="true" />本轮战报已送达，酒馆正在安排后续行动。</p>
        </section>
        <section class="campaign-chronicle" aria-label="战报档案">
          <button type="button" class="history-button" :aria-label="reportButtonLabel" :disabled="reportsLoading" :aria-expanded="reportTarget === selectedCampaign.id" aria-controls="campaign-reports" @click="toggleReports(selectedCampaign.id)"><span class="history-label"><ScrollText :width="19" :height="19" aria-hidden="true" /><span><strong>{{ reportButtonLabel }}</strong><small>每一次发现与行动，都有迹可循。</small></span></span><ChevronRight :width="18" :height="18" class="history-chevron" :class="{ expanded: reportTarget === selectedCampaign.id }" aria-hidden="true" /></button>
          <section v-if="reportTarget === selectedCampaign.id" id="campaign-reports" class="report-history" aria-label="历次战报" :aria-busy="reportsLoading">
            <p v-if="selectedCampaign.status === 'ended'" class="hall-muted">按发生顺序，回顾从最初发现到行动收尾的公开战报。</p>
            <p v-if="reportsLoading" class="hall-muted" role="status">正在读取战报…</p>
            <p v-if="reportError" class="hall-notice notice-error" role="alert">{{ reportError }}<button type="button" :disabled="reportsLoading" @click="loadReports(selectedCampaign.id, reportPage)">重试</button></p>
            <p v-if="!reportsLoading && !reportError && !reports.length" class="hall-muted report-empty">尚无已发布战报，静候小队带回消息。</p>
            <div class="report-timeline">
              <article v-for="report in reports" :key="report.revision" class="history-entry" :class="`outcome-${report.outcome}`"><span class="report-marker" aria-hidden="true" /><h6>{{ report.title }}</h6>
                <div class="report-meta"><span>{{ outcomeLabel[report.outcome] }} · {{ report.party_name || '探索小队' }}</span><button v-if="report.expedition_log_id" type="button" class="source-log-button" :aria-label="`${report.title}，查看来源探险日志 ${report.expedition_log_id}`" @click="emit('open-log', report.expedition_log_id)"><BookOpen :width="14" :height="14" aria-hidden="true" />来源：探险日志 #{{ report.expedition_log_id }}<ArrowUpRight :width="13" :height="13" aria-hidden="true" /></button></div>
                <p class="report-text">{{ report.summary }}</p>
              </article>
            </div>
            <nav v-if="reportPage > 1 || reportHasNext" class="hall-pagination" aria-label="战报分页"><button type="button" :disabled="reportsLoading || reportPage === 1" @click="loadReports(selectedCampaign.id, reportPage - 1)"><ChevronLeft :width="14" :height="14" aria-hidden="true" />上一页</button><span>第 {{ reportPage }} 页</span><button type="button" :disabled="reportsLoading || !reportHasNext" @click="loadReports(selectedCampaign.id, reportPage + 1)">下一页<ChevronRight :width="14" :height="14" aria-hidden="true" /></button></nav>
          </section>
        </section>
      </article>
    </div>
    <details v-if="hallBuilt" class="hall-guide">
      <summary><Info :width="16" :height="16" aria-hidden="true" /><span>参与须知</span><span class="guide-caption">招募、出发与报名规则</span><ChevronRight :width="15" :height="15" class="guide-chevron" aria-hidden="true" /></summary>
      <div class="guide-content">
        <ol class="guide-steps"><li><span>01</span><div><strong>选择行动</strong><p>了解当前局势，选择可前往的地图，接取并创建任务小队。</p></div></li><li><span>02</span><div><strong>集结出发</strong><p>到小队页招募并出发。同图可有多支小队探险，一队推进关键行动时，其他队伍继续探索。入口已更新或关闭时才取消本次出发并解散。</p></div></li><li><span>03</span><div><strong>关注后续</strong><p>战报记录实际经历。行动更新后，原报名不会自动接取下一项行动。</p></div></li></ol>
        <p v-if="store.campaignTaskPolicy" class="hall-muted">接取后 {{ store.campaignTaskPolicy.departure_timeout_minutes }} 分钟未出发，小队自动解散并恢复休息；队员随后 {{ store.campaignTaskPolicy.timeout_cooldown_minutes }} 分钟内不能接取或加入活动任务。</p><p class="hall-muted">取消报名会解散小队。</p>
      </div>
    </details>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  ArchiveOutline as Archive, OpenOutline as ArrowUpRight, BookOutline as BookOpen,
  CheckmarkOutline as Check, ChevronBackOutline as ChevronLeft, ChevronForwardOutline as ChevronRight,
  TimeOutline as Clock3, CompassOutline as Compass, FlagOutline as Flag,
  InformationCircleOutline as Info, LocationOutline as MapPin, RefreshOutline as RefreshCw,
  DocumentTextOutline as ScrollText, PeopleOutline as Users, CloseOutline as X,
} from '@vicons/ionicons5'
import api from '@/composables/useApi'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { useAdventurerS1Store } from '../stores/adventurerS1'
import { useLiveTick } from '../composables/useLiveTick'

interface Campaign {
  id: number; revision: number; title: string; summary: string; ended_at?: string | null
  objective: string; target_dungeons: string[]; status: 'open' | 'underway' | 'ended'
  isolated_world?: boolean
  core_task_dungeons?: string[]
  world_notice?: { title: string; summary: string; occurred_at: string }
  max_active_teams?: number; active_team_count?: number; busy_dungeons?: string[]
  dungeon_levels?: Record<string, { min_level: number; max_level: number; rank: number }>
  stage?: { title: string; status: 'active' | 'ready' | 'ended'; nodes: {
    dungeon: string; title: string; objective: string; result: 'pending' | 'confirmed' | 'failed'; required_for_success?: boolean
    evidence_log_id?: number | null
    challenge?: { recommended_level: number; brief: string }
  }[] }
}
interface Report {
  revision: number; title: string; summary: string; party_name: string
  expedition_log_id?: number | null
  outcome: 'discovered' | 'succeeded' | 'failed' | 'ongoing'
}
const emit = defineEmits<{ accepted: []; 'open-log': [logId: number] }>()
const store = useAdventurerS1Store()
const { now } = useLiveTick()
const taskCoolingDown = computed(() => !!store.campaignTaskCooldownUntil
  && Date.parse(store.campaignTaskCooldownUntil) > now.value + store.tavernServerOffsetMs)
const message = useGlobalMessage()
const items = ref<Campaign[]>([])
const selectedCampaignId = ref<number | null>(null)
const selectedCampaign = computed(() => items.value.find(item => item.id === selectedCampaignId.value))
const selectedAvailableMaps = computed(() => selectedCampaign.value ? availableMaps(selectedCampaign.value) : [])
const confirmedNodeCount = computed(() => selectedCampaign.value?.stage?.nodes.filter(node => node.result === 'confirmed').length || 0)
const reportButtonLabel = computed(() => reportTarget.value === selectedCampaignId.value
  ? '收起战报' : selectedCampaign.value?.status === 'ended' ? '回顾战役经过' : '查看历次战报')
const hallBuilt = ref(false)
const initializing = ref(true)
const loading = ref(false)
const errorText = ref('')
const page = ref(1)
const hasNext = ref(false)
const mapOptions = ref<string[]>([])
const selectableRanks = ref<number[]>([])
const selectedMaps = ref<Record<number, string>>({})
const selectedObjective = computed(() => {
  const campaign = selectedCampaign.value
  if (!campaign) return ''
  if (campaign.isolated_world && selectedMaps.value[campaign.id]
    && !campaign.core_task_dungeons?.includes(selectedMaps.value[campaign.id]!)) {
    return '探索这片土地现在的景象与变化，寻找当地仍可参与的冒险。'
  }
  return campaign.stage?.nodes.find(node => node.dungeon === selectedMaps.value[campaign.id])?.objective || campaign.objective
})
const accepting = ref<number | null>(null)
const reportTarget = ref<number | null>(null)
const reports = ref<Report[]>([])
const reportsLoading = ref(false)
const reportError = ref('')
const reportPage = ref(1)
const reportHasNext = ref(false)
const statusLabel = { open: '召集小队', underway: '队伍行动中 · 等待战报', ended: '事件已结束' }
const directoryStatus = { open: '待集结', underway: '行动中', ended: '已归档', preparing: '筹备中', waiting: '等待线索' }
const outcomeLabel = { discovered: '发现', succeeded: '目标达成', failed: '行动失利', ongoing: '继续调查' }
const nodeResultLabel = { pending: '等待探索', confirmed: '已经确认', failed: '行动受挫' }
function endedDate(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { hour12: false })
}
function campaignState(item: Campaign) {
  if (item.status === 'ended') return 'ended'
  if (item.stage?.status === 'ready') return 'preparing'
  if (item.stage && item.status === 'open' && !item.target_dungeons.length) return 'waiting'
  return item.status
}
function campaignStatus(item: Campaign) {
  const state = campaignState(item)
  if (state === 'preparing') return item.isolated_world && item.target_dungeons.length ? '后续行动筹备中 · 仍可探索' : '后续行动筹备中'
  if (state === 'waiting') return '等待新的线索'
  if (state === 'underway' && availableMaps(item).length) return '召集小队 · 关键行动进行中'
  return statusLabel[state]
}
function selectCampaign(id: number | null) {
  if (selectedCampaignId.value === id) return
  selectedCampaignId.value = id
  reportTarget.value = null
  reports.value = []
  reportPage.value = 1
  reportHasNext.value = false
  reportError.value = ''
}
let mounted = true
let timer: ReturnType<typeof setInterval> | undefined
function availableMaps(item: Campaign) {
  if (item.status === 'ended' || (item.stage?.status === 'ready' && !item.isolated_world)) return []
  return item.target_dungeons.filter(map => item.isolated_world
    ? selectableRanks.value.includes(item.dungeon_levels?.[map]?.rank ?? -1)
    : mapOptions.value.includes(map))
}
async function loadBoard(nextPage = 1) {
  if (loading.value) return
  loading.value = true
  errorText.value = ''
  try {
    const [{ data }] = await Promise.all([
      api.get('/adventurer-s1/campaign-hall', { params: { page: nextPage, per_page: 12 } }),
      store.fetchMyMatchTickets(),
    ])
    if (!mounted) return
    if (data.code !== 200) throw new Error(data.message || '进展读取失败')
    items.value = data.data.items
    page.value = data.data.page
    hasNext.value = data.data.has_next
    hallBuilt.value = data.data.hall_built
    for (const item of items.value) {
      const maps = availableMaps(item)
      if (!maps.includes(selectedMaps.value[item.id] || '')) selectedMaps.value[item.id] = maps[0] || ''
    }
    if (!items.value.some(item => item.id === selectedCampaignId.value)) {
      selectCampaign((items.value.find(item => item.status !== 'ended') || items.value[0])?.id ?? null)
    }
  } catch (error: any) {
    if (mounted) errorText.value = error?.response?.data?.message || error.message || '进展读取失败，请重试'
  } finally {
    if (mounted) loading.value = false
  }
}
async function loadReports(id: number, nextPage: number) {
  if (reportsLoading.value) return
  reportsLoading.value = true
  reportError.value = ''
  try {
    const { data } = await api.get('/adventurer-s1/campaign-hall/' + id + '/reports', { params: { page: nextPage, per_page: 12 } })
    if (!mounted || reportTarget.value !== id) return
    if (data.code !== 200) throw new Error(data.message || '战报读取失败')
    reports.value = data.data.items
    reportPage.value = data.data.page
    reportHasNext.value = data.data.has_next
  } catch (error: any) {
    if (mounted && reportTarget.value === id) reportError.value = error?.response?.data?.message || error.message || '战报读取失败'
  } finally {
    if (mounted) reportsLoading.value = false
  }
}
function toggleReports(id: number) {
  if (reportTarget.value === id) { reportTarget.value = null; return }
  reportTarget.value = id
  reports.value = []
  reportPage.value = 1
  reportHasNext.value = false
  void loadReports(id, 1)
}
async function accept(item: Campaign) {
  if (taskCoolingDown.value || accepting.value !== null || !selectedMaps.value[item.id]) return
  accepting.value = item.id
  try {
    const result = await store.createMatchmakingParty({
      preferredDungeon: selectedMaps.value[item.id],
      description: item.title,
      campaignDispatchId: item.id,
      campaignDispatchRevision: item.revision,
    })
    if (result.code !== 200) throw new Error(result.message || '接取失败')
    message.success('已接取行动，请在小队页招募并出发')
    emit('accepted')
  } catch (error: any) {
    message.error(error?.response?.data?.message || error.message || '接取失败')
    void store.fetchMyMatchTickets(true)
    void loadBoard(page.value)
  } finally {
    accepting.value = null
  }
}
onMounted(async () => {
  try {
    const { data } = await api.get('/adventurer-s1/matchmaking/available-dungeons')
    if (mounted && data.code === 200) {
      mapOptions.value = data.data.items.map((item: { name: string }) => item.name)
      selectableRanks.value = data.data.selectable_ranks || []
    }
  } catch {
    if (mounted) message.warning('可前往地图读取失败，请重新进入议事厅重试')
  }
  if (!mounted) return
  initializing.value = false
  await loadBoard()
  if (!mounted) return
  timer = setInterval(() => {
    if (document.visibilityState === 'visible') void loadBoard(page.value)
  }, 60_000)
})
onUnmounted(() => { mounted = false; if (timer) clearInterval(timer) })
</script>

<style scoped>
.campaign-hall {
  --hall-accent: var(--color-primary);
  display: grid;
  min-width: 0;
  gap: 20px;
  color: var(--color-text);
  font-size: 14px;
}
@supports (color: color-mix(in srgb, #000 50%, #fff)) {
  .campaign-hall { --hall-accent: color-mix(in srgb, #ad8657 70%, var(--color-text)); }
}
.campaign-hall *, .campaign-hall *::before, .campaign-hall *::after { box-sizing: border-box; }
.campaign-hall p, .campaign-hall h3, .campaign-hall h4, .campaign-hall h5, .campaign-hall h6 { margin: 0; }
.campaign-hall svg { flex-shrink: 0; }
button, select { font: inherit; }
button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-height: 40px; padding: 8px 12px; border: 1px solid var(--color-border); border-radius: 8px; color: var(--color-text); background: var(--color-surface); line-height: 1.5; cursor: pointer; transition: border-color .18s, background-color .18s; }
button:hover:not(:disabled) { border-color: var(--hall-accent); background: var(--color-hover); }
button:disabled { opacity: .5; cursor: default; }
button:focus-visible, select:focus-visible, summary:focus-visible { outline: 2px solid var(--hall-accent); outline-offset: 3px; }
.hall-masthead { position: relative; overflow: hidden; display: flex; align-items: center; gap: 18px; padding: 27px 28px; border: 1px solid var(--color-border); border-color: color-mix(in srgb, var(--hall-accent) 28%, var(--color-border)); border-radius: 16px; background: var(--color-surface); background: linear-gradient(115deg, color-mix(in srgb, var(--hall-accent) 9%, var(--color-surface)), var(--color-surface) 65%); }
.hall-emblem { display: grid; flex-shrink: 0; place-items: center; width: 60px; height: 60px; border: 1px solid var(--hall-accent); border-radius: 14px; color: var(--hall-accent); }
.hall-intro { position: relative; z-index: 1; flex: 1; min-width: 0; }
.hall-eyebrow { margin-bottom: 7px !important; color: var(--hall-accent); font-size: 11px; letter-spacing: .14em; }
.hall-intro h3 { font-family: "Noto Serif SC", "Songti SC", SimSun, serif; font-size: 25px; font-weight: 700; letter-spacing: .08em; line-height: 1.4; }
.hall-subtitle { margin-top: 8px !important; color: var(--color-text-secondary); font-size: 13px; line-height: 1.7; }
.refresh-button { position: relative; z-index: 1; flex-shrink: 0; font-size: 12px; }
.masthead-watermark { position: absolute; right: 14%; top: -25px; color: var(--hall-accent); opacity: .09; pointer-events: none; }
.hall-notice { display: flex; align-items: flex-start; gap: 10px; padding: 12px 15px; border: 1px solid var(--color-border); border-radius: 10px; font-size: 13px; line-height: 1.8; }
.hall-notice > svg { margin-top: 3px; }
.notice-error { color: var(--color-danger); background: var(--color-surface); background: color-mix(in srgb, var(--color-danger) 5%, var(--color-surface)); }
.notice-warning { color: var(--color-warning); background: var(--color-surface); background: color-mix(in srgb, var(--color-warning) 5%, var(--color-surface)); }
.hall-muted { color: var(--color-text-secondary); font-size: 12px; line-height: 1.8; }
.hall-board { display: grid; grid-template-columns: minmax(230px, 28%) minmax(0, 1fr); align-items: start; gap: 20px; min-width: 0; }
.campaign-directory { min-width: 0; }
.directory-heading { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 12px; padding: 0 2px; }
.directory-heading > span:first-child { display: flex; align-items: center; gap: 7px; font-size: 13px; font-weight: 600; }
.directory-heading svg { color: var(--hall-accent); }
.campaign-list { display: grid; gap: 10px; }
.campaign-choice { position: relative; display: grid; justify-content: stretch; gap: 9px; width: 100%; min-width: 0; padding: 16px; border-radius: 12px; text-align: left; }
.campaign-choice.selected { border-color: var(--hall-accent); background: var(--color-surface); background: color-mix(in srgb, var(--hall-accent) 7%, var(--color-surface)); box-shadow: inset 3px 0 var(--hall-accent); }
.choice-topline { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.choice-status, .hall-status { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; line-height: 1.6; }
.status-dot { flex-shrink: 0; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.tone-open { color: var(--hall-accent); }
.tone-underway { color: var(--color-primary); }
.tone-ended, .tone-waiting { color: var(--color-text-secondary); }
.tone-preparing { color: var(--color-warning); }
.choice-arrow { color: var(--color-text-secondary); }
.selected .choice-arrow { color: var(--hall-accent); }
.choice-title { font-size: 14px; line-height: 1.6; overflow-wrap: anywhere; }
.choice-summary { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; color: var(--color-text-secondary); font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.choice-location { display: flex; align-items: flex-start; gap: 5px; padding-top: 4px; color: var(--color-text-secondary); font-size: 11px; line-height: 1.7; overflow-wrap: anywhere; }
.choice-location svg { margin-top: 2px; }
.campaign-detail { min-width: 0; overflow-wrap: anywhere; border: 1px solid var(--color-border); border-radius: 16px; background: var(--color-surface); }
.campaign-heading { display: grid; gap: 14px; padding: 25px 26px 22px; border-bottom: 1px solid var(--color-border); }
.campaign-kicker { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; }
.campaign-kicker > span:first-child { display: inline-flex; align-items: center; gap: 6px; color: var(--hall-accent); font-size: 11px; letter-spacing: .08em; }
.hall-status { padding: 5px 9px; border: 1px solid var(--color-border); border-radius: 6px; background: var(--color-bg); }
.campaign-heading h4 { font-family: "Noto Serif SC", "Songti SC", SimSun, serif; font-size: 24px; line-height: 1.5; letter-spacing: .035em; }
.team-capacity { display: flex; align-items: flex-start; gap: 7px; color: var(--color-text-secondary); font-size: 12px; line-height: 1.7; }
.team-capacity svg { margin-top: 2px; }
.team-capacity b { color: var(--color-text); font-variant-numeric: tabular-nums; }
.campaign-brief, .campaign-ending, .stage-section { padding: 22px 26px; }
.section-caption { margin-bottom: 10px !important; color: var(--color-text-secondary); font-size: 12px; font-weight: 500; letter-spacing: .06em; }
.report-text { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; line-height: 1.95; }
.section-heading { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 16px; }
.section-heading h5 { display: flex; align-items: center; gap: 8px; font-size: 14px; line-height: 1.6; }
.section-heading h5 svg { color: var(--hall-accent); }
.campaign-ending { background: var(--color-bg); background: color-mix(in srgb, var(--hall-accent) 5%, var(--color-surface)); }
.current-action { margin: 0 26px 4px; padding: 20px; border: 1px solid var(--color-border); border-color: color-mix(in srgb, var(--hall-accent) 35%, var(--color-border)); border-radius: 12px; background: var(--color-bg); background: color-mix(in srgb, var(--hall-accent) 4%, var(--color-bg)); }
.action-caption { color: var(--hall-accent); font-size: 11px; }
.action-objective { font-weight: 500; }
.action-maps { display: flex; align-items: flex-start; gap: 6px; margin-top: 10px !important; color: var(--color-text-secondary); font-size: 12px; line-height: 1.8; }
.action-maps svg { margin-top: 4px; }
.hall-actions { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 10px; margin-top: 18px; }
.map-field { display: grid; flex: 1 1 200px; min-width: 0; gap: 7px; color: var(--color-text-secondary); font-size: 12px; }
select { width: 100%; min-width: 0; min-height: 44px; padding: 10px 12px; border: 1px solid var(--color-border); border-radius: 8px; color: var(--color-text); background: var(--color-surface); }
select:disabled { opacity: .6; }
.accept-button { flex: 1 1 150px; min-height: 44px; border-color: var(--hall-accent); color: var(--color-bg); background: var(--hall-accent); font-size: 13px; font-weight: 600; }
.accept-button:hover:not(:disabled) { color: var(--color-bg); background: var(--hall-accent); filter: brightness(1.1); }
.action-note { margin-top: 12px !important; color: var(--color-text-secondary); font-size: 12px; line-height: 1.8; }
.current-action > .hall-muted { margin-top: 7px; }
.departure-policy { display: flex; align-items: flex-start; gap: 7px; margin-top: 14px !important; padding-top: 12px; border-top: 1px solid var(--color-border); color: var(--color-text-secondary); font-size: 12px; line-height: 1.8; }
.departure-policy svg { margin-top: 3px; color: var(--hall-accent); }
.stage-title { margin-top: 5px !important; color: var(--color-text-secondary); font-size: 12px; line-height: 1.7; }
.stage-count { color: var(--color-text-secondary); font-size: 11px; font-variant-numeric: tabular-nums; }
.stage-count b { color: var(--color-text); }
.stage-progress { height: 4px; margin: 0 0 18px; overflow: hidden; border-radius: 4px; background: var(--color-border); }
.stage-progress > span { display: block; height: 100%; border-radius: inherit; background: var(--hall-accent); }
.stage-nodes { display: grid; gap: 10px; }
.stage-node { display: flex; align-items: flex-start; gap: 12px; min-width: 0; padding: 16px; border: 1px solid var(--color-border); border-radius: 10px; background: var(--color-bg); }
.node-marker { display: grid; place-items: center; flex-shrink: 0; width: 27px; height: 27px; border: 1px solid var(--color-border); border-radius: 50%; color: var(--color-text-secondary); font-size: 11px; font-variant-numeric: tabular-nums; }
.node-confirmed .node-marker, .node-confirmed .node-result { color: var(--color-success); }
.node-failed .node-marker, .node-failed .node-result { color: var(--color-danger); }
.node-body { flex: 1; min-width: 0; }
.node-heading { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 7px; margin: 3px 0 5px; color: var(--color-text-secondary); font-size: 11px; line-height: 1.6; }
.node-body h6 { font-size: 14px; line-height: 1.7; }
.optional-label { display: inline-block; margin: 4px 0; padding: 1px 6px; border: 1px solid var(--color-border); border-radius: 4px; color: var(--color-text-secondary); font-size: 10px; }
.node-objective { margin-top: 5px !important; color: var(--color-text-secondary); font-size: 12px; }
.node-challenge { margin-top: 9px !important; color: var(--color-text-secondary); font-size: 12px; line-height: 1.8; }
.node-challenge > span { color: var(--hall-accent); }
.source-log-button { justify-content: flex-start; min-height: 32px; max-width: 100%; margin-top: 5px; padding: 4px 0; border: 0; color: var(--color-primary); background: transparent; font-size: 12px; line-height: 1.7; text-align: left; overflow-wrap: anywhere; }
.source-log-button:hover:not(:disabled) { color: var(--color-primary); background: transparent; text-decoration: underline; text-underline-offset: 3px; }
.stage-ready { display: flex; align-items: flex-start; gap: 7px; margin-top: 14px !important; color: var(--color-text-secondary); font-size: 12px; line-height: 1.8; }
.stage-ready svg { margin-top: 3px; color: var(--color-warning); }
.campaign-chronicle { padding: 0 26px 24px; }
.history-button { display: flex; justify-content: space-between; width: 100%; min-width: 0; gap: 15px; padding: 17px 0 0; border: 0; border-top: 1px solid var(--color-border); border-radius: 0; background: transparent; text-align: left; }
.history-button:hover:not(:disabled) { border-color: var(--color-border); background: transparent; color: var(--hall-accent); }
.history-label { display: flex; align-items: center; gap: 11px; min-width: 0; }
.history-label > svg { color: var(--hall-accent); }
.history-label strong { display: block; font-size: 13px; font-weight: 500; }
.history-label small { display: block; margin-top: 4px; color: var(--color-text-secondary); font-size: 11px; }
.history-chevron { color: var(--color-text-secondary); transition: transform .18s; }
.history-chevron.expanded { transform: rotate(90deg); }
.report-history { padding-top: 20px; }
.report-history > .hall-notice { align-items: center; justify-content: space-between; }
.report-empty { padding: 10px 0; }
.report-timeline { margin-top: 12px; padding-left: 7px; }
.history-entry { position: relative; padding: 0 0 24px 23px; border-left: 1px solid var(--color-border); }
.history-entry:last-child { padding-bottom: 3px; }
.report-marker { position: absolute; top: 7px; left: -4px; width: 7px; height: 7px; border-radius: 50%; background: var(--hall-accent); box-shadow: 0 0 0 4px var(--color-surface); }
.outcome-succeeded .report-marker { background: var(--color-success); }
.outcome-failed .report-marker { background: var(--color-danger); }
.history-entry h6 { font-size: 14px; line-height: 1.7; }
.report-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 3px 12px; margin: 5px 0 10px; color: var(--color-text-secondary); font-size: 11px; line-height: 1.8; }
.report-meta .source-log-button { margin: 0; }
.history-entry .report-text { font-size: 13px; }
.hall-pagination { display: flex; flex-wrap: wrap; align-items: center; justify-content: center; gap: 10px; margin-top: 20px; color: var(--color-text-secondary); font-size: 11px; }
.hall-pagination button { min-height: 36px; padding: 6px 9px; font-size: 11px; }
.hall-guide { min-width: 0; border: 1px solid var(--color-border); border-radius: 12px; color: var(--color-text-secondary); background: var(--color-surface); }
.hall-guide summary { display: flex; align-items: center; gap: 8px; min-height: 48px; padding: 14px 18px; list-style: none; color: var(--color-text); font-size: 12px; cursor: pointer; }
.hall-guide summary::-webkit-details-marker { display: none; }
.hall-guide summary > svg:first-child { color: var(--hall-accent); }
.guide-caption { flex: 1; color: var(--color-text-secondary); font-size: 11px; }
.guide-chevron { margin-left: auto; }
.hall-guide[open] .guide-chevron { transform: rotate(90deg); }
.guide-content { display: grid; gap: 10px; padding: 4px 18px 18px; }
.guide-steps { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px; margin: 0 0 8px; padding: 0; list-style: none; }
.guide-steps li { display: flex; align-items: flex-start; gap: 10px; min-width: 0; font-size: 12px; line-height: 1.8; }
.guide-steps li > span { color: var(--hall-accent); font-size: 11px; font-variant-numeric: tabular-nums; }
.guide-steps strong { color: var(--color-text); font-weight: 500; }
.guide-steps p { margin-top: 4px; }
.hall-empty { display: grid; justify-items: center; gap: 14px; padding: 60px 24px; border: 1px dashed var(--color-border); border-radius: 16px; color: var(--color-text-secondary); text-align: center; }
.hall-empty > svg { color: var(--hall-accent); opacity: .7; }
.hall-empty h4 { color: var(--color-text); font-size: 16px; font-weight: 500; }
.hall-empty p { max-width: 410px; font-size: 13px; line-height: 1.9; }
.loading-lines { display: grid; gap: 9px; width: 200px; }
.loading-lines span { height: 9px; border-radius: 5px; background: var(--color-border); }
.loading-lines span:nth-child(2) { width: 85%; }
.loading-lines span:nth-child(3) { width: 65%; }
.spinning { animation: hall-spin 1.2s linear infinite; }
@keyframes hall-spin { to { transform: rotate(360deg); } }
@media (max-width: 900px) {
  .hall-board { grid-template-columns: minmax(205px, 29%) minmax(0, 1fr); gap: 14px; }
  .campaign-heading, .campaign-brief, .campaign-ending, .stage-section { padding-right: 20px; padding-left: 20px; }
  .current-action { margin-right: 20px; margin-left: 20px; padding: 16px; }
  .campaign-chronicle { padding-right: 20px; padding-left: 20px; }
  .campaign-heading h4 { font-size: 21px; }
  .action-caption { display: none; }
}
@media (max-width: 700px) {
  .campaign-hall { gap: 16px; }
  .hall-masthead { flex-wrap: wrap; gap: 13px; padding: 20px; }
  .hall-emblem { width: 46px; height: 46px; border-radius: 11px; }
  .hall-emblem svg { width: 25px; height: 25px; }
  .hall-intro h3 { font-size: 22px; }
  .hall-subtitle { font-size: 12px; }
  .refresh-button { margin-left: auto; min-height: 36px; padding: 6px 10px; }
  .masthead-watermark { right: -20px; }
  .hall-board { grid-template-columns: minmax(0, 1fr); gap: 16px; }
  .campaign-list { display: flex; gap: 10px; overflow-x: auto; padding: 3px 3px 10px; scroll-snap-type: x proximity; scrollbar-width: thin; }
  .campaign-choice { flex: 0 0 230px; scroll-snap-align: start; padding: 14px; }
  .choice-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .choice-location { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 1; overflow: hidden; }
  .choice-location svg { display: none; }
  .directory-pagination { margin-top: 5px; }
  .campaign-kicker { align-items: flex-start; }
  .campaign-heading { gap: 12px; }
  .stage-node { gap: 9px; padding: 14px 12px; }
  .node-heading { align-items: flex-start; }
  .source-log-button { min-height: 40px; }
  .guide-steps { grid-template-columns: minmax(0, 1fr); gap: 12px; }
  .guide-caption { display: none; }
}
@media (max-width: 400px) {
  .hall-masthead { padding: 18px 16px; }
  .hall-emblem { display: none; }
  .hall-eyebrow { font-size: 10px; }
  .hall-intro { flex-basis: 100%; }
  .campaign-heading, .campaign-brief, .campaign-ending, .stage-section { padding-right: 16px; padding-left: 16px; }
  .current-action { margin-right: 16px; margin-left: 16px; padding: 14px; }
  .campaign-chronicle { padding-right: 16px; padding-left: 16px; }
  .hall-actions > label, .hall-actions > button { flex-basis: 100%; }
}
@media (prefers-reduced-motion: reduce) {
  .campaign-hall *, .campaign-hall *::before, .campaign-hall *::after { animation: none !important; transition: none !important; }
}
</style>
