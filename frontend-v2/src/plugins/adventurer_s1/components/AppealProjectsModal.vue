<template>
  <Teleport to="body">
    <div class="project-overlay s1-mobile-surface s1-mobile-overlay" @click.self="emit('close')">
      <section class="project-modal s1-mobile-dialog" role="dialog" aria-modal="true" aria-labelledby="appeal-project-title">
        <header class="project-head s1-mobile-dialog-header">
          <div>
            <p class="project-kicker">STRUCTURED APPEAL PROJECTS</p>
            <h3 id="appeal-project-title">申诉专项项目</h3>
          </div>
          <button type="button" class="close-btn" aria-label="关闭" @click="emit('close')">✕</button>
        </header>

        <div class="project-body s1-mobile-dialog-body">
          <div v-if="loading" class="state-card">正在核对历史流水…</div>
          <div v-else-if="loadError" class="state-card error-state">
            <span>{{ loadError }}</span>
            <button type="button" class="minor-btn" @click="loadOverview">重新核对</button>
          </div>

          <template v-else-if="overview">
            <div class="balance-strip">
              <span>当前可用金币</span>
              <strong>💰 {{ formatMoney(overview.gold) }}</strong>
            </div>

            <article class="project-card integrity-card">
              <div class="project-title-row">
                <span class="project-icon">🛡️</span>
                <div>
                  <h4>装备聚合自检</h4>
                  <p>不调用 AI，只按历次探险冻结快照核对；探险后已有申诉或内容变更的装备会保守跳过。</p>
                </div>
                <span class="count-badge">{{ integrityPreview?.summary.candidate_count ?? 0 }} 件</span>
              </div>

              <div v-if="integrityLoading" class="inline-state">正在扫描装备血缘…</div>
              <div v-else-if="integrityError" class="inline-state error-state">
                {{ integrityError }}
              </div>
              <template v-else-if="integrityPreview">
                <div v-if="integrityPreview.items.length" class="record-list integrity-list">
                  <div
                    v-for="(item, index) in integrityPreview.items"
                    :key="`${item.source_log_id}-${item.equipment_name}-${index}`"
                    class="record-row integrity-row"
                  >
                    <div class="record-main">
                      <div class="item-line">
                        <strong>{{ item.equipment_name }}</strong>
                        <span class="quality-chip exact">
                          {{ item.repair_kind === 'identity_binding' ? '稳定 ID 错绑' : '可唯一还原' }}
                        </span>
                      </div>
                      <p>
                        {{ item.adventurer_name || '我的冒险者' }} · {{ containerLabel(item.container) }}
                        · 来源日志 #{{ item.source_log_id }}
                      </p>
                      <p class="integrity-change-line">
                        修复：{{ item.repair_fields.map(repairFieldLabel).join('、') }}
                      </p>
                      <p v-if="item.repair_fields.includes('name')" class="integrity-change-line">
                        名称 {{ item.before_name }} → {{ item.repair_name }}
                      </p>
                      <p v-if="item.repair_fields.includes('type')" class="integrity-change-line">
                        类型 {{ equipmentTypeLabel(item.before_type) }} → {{ equipmentTypeLabel(item.repair_type) }}
                      </p>
                      <p v-if="item.repair_fields.includes('rarity')" class="integrity-change-line">
                        稀有度 {{ rarityLabel(item.before_rarity) }} → {{ rarityLabel(item.repair_rarity) }}
                      </p>
                      <p v-if="item.repair_fields.includes('evolution_stage')" class="integrity-change-line">
                        伙伴阶级 {{ item.before_evolution_stage }} → {{ item.repair_evolution_stage }}
                      </p>
                      <p v-if="item.repair_fields.includes('uses_left')" class="integrity-change-line">
                        可用次数 {{ usesLeftLabel(item.before_uses_left) }} → {{ usesLeftLabel(item.repair_uses_left) }}
                      </p>
                      <p v-if="item.repair_fields.includes('recovery_minutes')" class="integrity-change-line">
                        固定恢复时长 {{ recoveryMinutesLabel(item.before_recovery_minutes) }} → {{ recoveryMinutesLabel(item.repair_recovery_minutes) }}
                      </p>
                      <details v-if="item.repair_fields.includes('custom_desc')" class="integrity-description">
                        <summary>查看描述变化（{{ item.before_description_length }} → {{ item.repair_description_length }} 字）</summary>
                        <strong>当前描述</strong>
                        <p>{{ item.before_description ?? '描述暂未提供，请更新后重新打开核对。' }}</p>
                        <strong>修复后描述</strong>
                        <p>{{ item.repair_description ?? '描述暂未提供，请更新后重新打开核对。' }}</p>
                      </details>
                    </div>
                  </div>
                </div>
                <p v-else class="empty-copy">没有发现能由完整历史证据唯一还原的装备脏数据。</p>

                <p v-if="integritySkippedCount > 0" class="safety-note warning-note">
                  另有 {{ integritySkippedCount }} 条历史异常因物品已消耗、状态变化、后续申诉或血缘不唯一而跳过，系统不会猜测修复。
                </p>
                <p v-if="integrityPreview.summary.scan_truncated" class="safety-note warning-note">
                  历史日志超过本次扫描上限；更早记录没有纳入自动修复。
                </p>
                <p class="safety-note">
                  普通污染只修复类型、伙伴阶级和丢失描述；若稳定 ID 被写成同场另一件出发装备，则恢复该 ID 的冻结身份状态。执行前会再次锁定并核对当前状态，不消耗申诉额度。
                </p>
                <p v-if="integrityPreview.items.length && !integrityPreview.execution_available" class="safety-note warning-note">
                  本次核对结果暂时无法用于执行修复。你仍可查看上方的修复前后内容，装备尚未改变。请稍后重新核对；若持续出现，请反馈分区、角色名和出现时间。
                </p>
                <div class="integrity-actions">
                  <button
                    type="button"
                    class="primary-btn"
                    :disabled="integrityExecuting || !integrityPreview.execution_available || !integrityPreview.preview_token || integrityPreview.items.length <= 0"
                    @click="executeIntegrityRepair"
                  >
                    {{ integrityExecuting ? '正在逐条复核并修复…' : '修复全部可确认装备' }}
                  </button>
                  <button
                    v-if="integrityPreview.restore_available"
                    type="button"
                    class="minor-btn restore-btn"
                    :disabled="integrityRestoring || integrityExecuting"
                    @click="restoreIntegrityRepair"
                  >
                    {{ integrityRestoring ? '正在条件撤销…' : '撤销上一次装备修复' }}
                  </button>
                </div>
              </template>
            </article>

            <article v-if="mutationProject" class="project-card mutation-card">
              <div class="project-title-row">
                <span class="project-icon">🧬</span>
                <div>
                  <h4>{{ mutationProject.title }}</h4>
                  <p>{{ mutationProject.description }}</p>
                </div>
                <span class="count-badge">{{ mutationProject.available_count }} 条</span>
              </div>

              <div v-if="mutationProject.items.length" class="record-list">
                <div v-for="(item, index) in mutationProject.items" :key="`${item.name}-${item.occurred_at}-${index}`" class="record-row">
                  <div>
                    <strong>{{ item.name }}<span v-if="item.quantity > 1"> ×{{ item.quantity }}</span></strong>
                    <p>{{ rarityLabel(item.rarity) }} · {{ formatWhen(item.occurred_at) }}</p>
                  </div>
                  <span class="status-chip">待补退</span>
                </div>
              </div>
              <p v-else class="empty-copy">没有发现“已退金币与精力、但漏退辅助物品”的旧记录。</p>

              <p class="safety-note">仅处理能由消耗、技术中止退款和缺失结算三段流水共同确认的记录；正常嬗变失败不会退素材。</p>
              <button
                type="button"
                class="primary-btn"
                :disabled="mutationLoading || mutationProject.available_count <= 0"
                @click="claimMutationRefunds"
              >
                {{ mutationLoading ? '正在逐条补退…' : '补退全部可确认物品' }}
              </button>
            </article>

            <article v-if="buybackProject" class="project-card buyback-card">
              <div class="project-title-row">
                <span class="project-icon">⚖️</span>
                <div>
                  <h4>{{ buybackProject.title }}</h4>
                  <p>{{ buybackProject.description }}</p>
                </div>
                <span class="count-badge">{{ buybackProject.available_count }} 件</span>
              </div>

              <div v-if="buybackProject.items.length" class="record-list buyback-list">
                <div v-for="item in buybackProject.items" :key="item.token" class="record-row buyback-row">
                  <div class="record-main">
                    <div class="item-line">
                      <strong>{{ item.name }}<span v-if="item.quantity > 1"> ×{{ item.quantity }}</span></strong>
                      <span v-if="item.snapshot_quality === 'exact'" class="quality-chip exact">完整存档</span>
                      <span v-else class="quality-chip legacy">旧流水重建</span>
                    </div>
                    <p>{{ rarityLabel(item.rarity) }} · 售于 {{ formatWhen(item.sold_at) }}</p>
                    <p class="deadline-line" :class="{ expired: isExpired(item) }">
                      <template v-if="isExpired(item)">买回期限已结束</template>
                      <template v-else>
                        剩余 {{ formatRemaining(item) }} · 截止 {{ formatWhen(item.expires_at) }}
                      </template>
                    </p>
                    <p class="price-line">
                      原收购价 {{ formatMoney(item.sale_total ?? 0) }} 金币
                      <span>→</span>
                      买回价 <strong>{{ formatMoney(item.repurchase_cost ?? 0) }}</strong> 金币
                    </p>
                  </div>
                  <button
                    type="button"
                    class="repurchase-btn"
                    :disabled="buybackLoadingToken === item.token || isExpired(item) || overview.gold + 0.001 < (item.repurchase_cost ?? 0)"
                    @click="repurchase(item)"
                  >
                    <template v-if="buybackLoadingToken === item.token">买回中…</template>
                    <template v-else-if="isExpired(item)">已过期</template>
                    <template v-else-if="overview.gold + 0.001 < (item.repurchase_cost ?? 0)">金币不足</template>
                    <template v-else>买回</template>
                  </button>
                </div>
              </div>
              <p v-else class="empty-copy">近期没有可自动核对并买回的收购商行出售记录。</p>

              <p class="safety-note">每条出售流水仅保留 {{ buybackProject.repurchase_hours ?? 24 }} 小时买回机会。扣款、还物和买回记录在同一事务完成；任一步失败都不会扣钱。</p>
            </article>
          </template>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { formatMoney } from '@/utils/format'
import { formatDateTime } from '@/utils/time'
import { useAdventurerS1Store } from '../stores/adventurerS1'

interface ProjectItem {
  token?: string
  name: string
  rarity: string
  quantity: number
  occurred_at?: string | null
  sold_at?: string | null
  expires_at?: string | null
  sale_total?: number
  repurchase_cost?: number
  snapshot_quality?: 'exact' | 'catalog_reconstructed'
}

interface AppealProject {
  code: 'pet_evolution_refund' | 'buyback_repurchase'
  title: string
  description: string
  available_count: number
  markup_percent?: number
  repurchase_hours?: number
  items: ProjectItem[]
}

interface AppealProjectsOverview {
  gold: number
  projects: AppealProject[]
}

interface EquipmentIntegrityItem {
  source_log_id: number
  adventurer_name: string
  equipment_name: string
  container: 'equipment' | 'warehouse'
  repair_kind: 'protected_state' | 'identity_binding'
  repair_fields: Array<'name' | 'type' | 'rarity' | 'custom_desc' | 'uses_left' | 'evolution_stage' | 'recovery_minutes'>
  before_name: string
  repair_name: string
  before_type: string
  repair_type: string
  before_rarity: string
  repair_rarity: string
  before_uses_left: number | null
  repair_uses_left: number | null
  before_evolution_stage: number
  repair_evolution_stage: number
  before_recovery_minutes: number
  repair_recovery_minutes: number
  before_description_length: number
  repair_description_length: number
  before_description?: string
  repair_description?: string
}

interface EquipmentIntegrityPreview {
  summary: {
    scanned_log_count: number
    scan_truncated: boolean
    candidate_count: number
    lineage_ambiguous: number
    lineage_changed: number
    current_ambiguous: number
    current_missing_or_changed: number
    post_source_appeal_mutation: number
    identity_binding_ambiguous: number
  }
  items: EquipmentIntegrityItem[]
  preview_token?: string | null
  execution_available: boolean
  restore_available: boolean
}

const emit = defineEmits<{ close: []; changed: [] }>()
const store = useAdventurerS1Store()
const message = useGlobalMessage()

const overview = ref<AppealProjectsOverview | null>(null)
const loading = ref(false)
const loadError = ref('')
const integrityPreview = ref<EquipmentIntegrityPreview | null>(null)
const integrityLoading = ref(false)
const integrityError = ref('')
const integrityExecuting = ref(false)
const integrityRestoring = ref(false)
const mutationLoading = ref(false)
const buybackLoadingToken = ref<string | null>(null)
const nowMs = ref(Date.now())
let deadlineTimer: ReturnType<typeof setInterval> | null = null

const mutationProject = computed(() => (
  overview.value?.projects.find(project => project.code === 'pet_evolution_refund') ?? null
))
const buybackProject = computed(() => (
  overview.value?.projects.find(project => project.code === 'buyback_repurchase') ?? null
))
const integritySkippedCount = computed(() => {
  const summary = integrityPreview.value?.summary
  if (!summary) return 0
  return Number(summary.lineage_ambiguous || 0)
    + Number(summary.lineage_changed || 0)
    + Number(summary.current_ambiguous || 0)
    + Number(summary.current_missing_or_changed || 0)
    + Number(summary.post_source_appeal_mutation || 0)
    + Number(summary.identity_binding_ambiguous || 0)
})

function rarityLabel(rarity: string) {
  return ({
    common: '普通',
    rare: '稀有',
    epic: '史诗',
    legendary: '传说',
  } as Record<string, string>)[rarity] || rarity || '普通'
}

function formatWhen(value?: string | null) {
  return value ? formatDateTime(value) : '时间未知'
}

function containerLabel(container: EquipmentIntegrityItem['container']) {
  return container === 'warehouse' ? '收藏室' : '随身装备'
}

function repairFieldLabel(field: EquipmentIntegrityItem['repair_fields'][number]) {
  return ({
    name: '稳定名称',
    type: '装备类型',
    rarity: '稀有度',
    evolution_stage: '伙伴阶级',
    custom_desc: '丢失描述',
    uses_left: '可用次数',
    recovery_minutes: '固定恢复时长',
  } as const)[field]
}

function usesLeftLabel(value: number | null) {
  return value == null ? '不限次数' : `${value} 次`
}

function recoveryMinutesLabel(value: number) {
  const minutes = Math.max(0, Number(value || 0))
  return minutes > 0 ? `${minutes} 分钟` : '无固定恢复'
}

function equipmentTypeLabel(type: string) {
  return ({
    companion: '伙伴',
    weapon: '武器',
    armor: '防具',
    accessory: '饰品',
    consumable: '消耗品',
  } as Record<string, string>)[type] || type || '未分类'
}

function expiresAtMs(item: ProjectItem) {
  const value = item.expires_at ? Date.parse(item.expires_at) : Number.NaN
  return Number.isFinite(value) ? value : 0
}

function isExpired(item: ProjectItem) {
  const deadline = expiresAtMs(item)
  return deadline <= 0 || deadline <= nowMs.value
}

function formatRemaining(item: ProjectItem) {
  const remainingMs = Math.max(0, expiresAtMs(item) - nowMs.value)
  const totalMinutes = Math.ceil(remainingMs / 60_000)
  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  return hours > 0 ? `${hours} 小时 ${minutes} 分钟` : `${minutes} 分钟`
}

async function loadOverview() {
  loading.value = true
  loadError.value = ''
  try {
    const [res] = await Promise.all([
      store.fetchAppealProjects(),
      loadIntegrityPreview(),
    ])
    if (res?.code !== 200 || !res.data) {
      throw new Error(res?.message || '专项项目加载失败')
    }
    overview.value = res.data as AppealProjectsOverview
  } catch (error: any) {
    overview.value = null
    loadError.value = error?.response?.data?.message || error?.message || '专项项目加载失败'
  } finally {
    loading.value = false
  }
}

async function loadIntegrityPreview() {
  integrityLoading.value = true
  integrityError.value = ''
  try {
    const res = await store.previewEquipmentIntegrityAppealProject()
    if (res?.code !== 200 || !res.data) {
      throw new Error(res?.message || '装备自检失败')
    }
    integrityPreview.value = res.data as EquipmentIntegrityPreview
  } catch (error: any) {
    integrityPreview.value = null
    integrityError.value = error?.response?.data?.message || error?.message || '装备自检失败'
  } finally {
    integrityLoading.value = false
  }
}

async function executeIntegrityRepair() {
  const token = integrityPreview.value?.preview_token
  if (!token || integrityExecuting.value) return
  if (!confirm('仅修复完整历史证据能唯一定位的装备，并保留后续合法变化。确认执行？')) return
  integrityExecuting.value = true
  try {
    const res = await store.executeEquipmentIntegrityAppealProject(token)
    if (res?.code !== 200 || !res.data) {
      throw new Error(res?.message || '装备聚合修复失败')
    }
    const repaired = Number(res.data.repaired_count || 0)
    const skipped = Number(res.data.skipped_state_drifted || 0)
    if (repaired > 0) {
      message.success(`已修复 ${repaired} 件装备${skipped > 0 ? `，另有 ${skipped} 件因状态变化跳过` : ''}`)
      emit('changed')
    } else if (skipped > 0) {
      message.error('装备状态已变化，本次没有自动修改；请重新扫描')
    } else {
      message.success('没有需要修复的装备')
    }
    await loadOverview()
  } catch (error: any) {
    message.error(error?.response?.data?.message || error?.message || '装备聚合修复失败')
  } finally {
    integrityExecuting.value = false
  }
}

async function restoreIntegrityRepair() {
  if (integrityRestoring.value) return
  if (!confirm('仅当装备仍保持修复后状态时才会撤销。确认撤销上一次装备修复？')) return
  integrityRestoring.value = true
  try {
    const res = await store.restoreEquipmentIntegrityAppealProject()
    if (res?.code !== 200 || !res.data) {
      throw new Error(res?.message || '撤销装备修复失败')
    }
    const restored = Number(res.data.restored_count || 0)
    const skipped = Number(res.data.skipped_state_drifted || 0)
    if (restored > 0) {
      message.success(`已撤销 ${restored} 件装备的上一次修复${skipped > 0 ? `，另有 ${skipped} 件因状态变化跳过` : ''}`)
      emit('changed')
    } else {
      message.error(skipped > 0 ? '装备状态已变化，未执行撤销' : '没有可撤销的装备修复')
    }
    await loadOverview()
  } catch (error: any) {
    message.error(error?.response?.data?.message || error?.message || '撤销装备修复失败')
  } finally {
    integrityRestoring.value = false
  }
}

async function claimMutationRefunds() {
  if (mutationLoading.value) return
  mutationLoading.value = true
  try {
    const res = await store.claimPetEvolutionAppealProject()
    if (res?.code !== 200 || !res.data) {
      throw new Error(res?.message || '嬗变补退失败')
    }
    const refunded = Number(res.data.refunded_count || 0)
    const failed = Number(res.data.failed_count || 0)
    if (refunded > 0) {
      message.success(res.message || res.data.message || `已补退 ${refunded} 条物品`)
      emit('changed')
    } else if (failed > 0) {
      message.error(res.data.message || '没有记录能完成自动补退')
    } else {
      message.success(res.data.message || '没有发现需要补退的记录')
    }
    await loadOverview()
  } catch (error: any) {
    message.error(error?.response?.data?.message || error?.message || '嬗变补退失败')
  } finally {
    mutationLoading.value = false
  }
}

async function repurchase(item: ProjectItem) {
  const token = item.token
  if (!token || buybackLoadingToken.value || isExpired(item)) return
  buybackLoadingToken.value = token
  try {
    const res = await store.repurchaseBuybackAppealProject(token)
    if (res?.code !== 200 || !res.data) {
      throw new Error(res?.message || '物品买回失败')
    }
    message.success(res.message || res.data.message || `已买回「${item.name}」`)
    emit('changed')
    await loadOverview()
  } catch (error: any) {
    message.error(error?.response?.data?.message || error?.message || '物品买回失败')
  } finally {
    buybackLoadingToken.value = null
  }
}

onMounted(() => {
  void loadOverview()
  deadlineTimer = setInterval(() => {
    nowMs.value = Date.now()
  }, 30_000)
})

onBeforeUnmount(() => {
  if (deadlineTimer !== null) clearInterval(deadlineTimer)
})
</script>

<style scoped>
.project-overlay {
  position: fixed;
  inset: 0;
  z-index: 1200;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgba(4, 7, 14, 0.78);
  backdrop-filter: blur(7px);
}

.project-modal {
  width: min(760px, 100%);
  max-height: min(88vh, 900px);
  overflow: hidden;
  border: 1px solid rgba(220, 178, 92, 0.38);
  border-radius: 18px;
  background: linear-gradient(155deg, rgba(31, 27, 30, 0.99), rgba(16, 20, 29, 0.99));
  color: #f4ead8;
  box-shadow: 0 28px 80px rgba(0, 0, 0, 0.55), inset 0 1px rgba(255, 255, 255, 0.04);
}

.project-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 20px 22px 16px;
  border-bottom: 1px solid rgba(220, 178, 92, 0.2);
}

.project-kicker {
  margin: 0 0 4px;
  color: #b9a985;
  font-size: 0.68rem;
  letter-spacing: 0.16em;
}

.project-head h3 {
  margin: 0;
  font-family: Georgia, 'Noto Serif SC', serif;
  font-size: 1.35rem;
}

.close-btn,
.minor-btn,
.primary-btn,
.repurchase-btn {
  border: 0;
  cursor: pointer;
  color: inherit;
}

.close-btn {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.07);
}

.project-body {
  max-height: calc(min(88vh, 900px) - 78px);
  overflow-y: auto;
  padding: 18px 22px 24px;
}

.state-card,
.balance-strip,
.project-card {
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.035);
}

.state-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 18px;
}

.error-state {
  color: #ffc7bd;
}

.minor-btn {
  padding: 8px 12px;
  border-radius: 9px;
  background: rgba(220, 178, 92, 0.16);
}

.balance-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 15px;
  margin-bottom: 14px;
  color: #cfc3ae;
}

.balance-strip strong {
  color: #f3c969;
}

.project-card {
  padding: 16px;
}

.project-card + .project-card {
  margin-top: 15px;
}

.mutation-card {
  border-color: rgba(126, 168, 239, 0.25);
}

.integrity-card {
  border-color: rgba(91, 181, 155, 0.3);
}

.buyback-card {
  border-color: rgba(220, 178, 92, 0.28);
}

.project-title-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: start;
  gap: 12px;
}

.project-icon {
  display: grid;
  place-items: center;
  width: 42px;
  height: 42px;
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.06);
  font-size: 1.35rem;
}

.project-title-row h4 {
  margin: 0 0 5px;
  font-size: 1.03rem;
}

.project-title-row p,
.record-row p,
.safety-note,
.empty-copy {
  margin: 0;
  color: #ab9f8f;
  font-size: 0.82rem;
  line-height: 1.55;
}

.count-badge,
.status-chip,
.quality-chip {
  border-radius: 999px;
  white-space: nowrap;
  font-size: 0.7rem;
}

.count-badge {
  padding: 5px 9px;
  background: rgba(220, 178, 92, 0.13);
  color: #e7c77f;
}

.record-list {
  display: grid;
  gap: 8px;
  margin-top: 14px;
}

.inline-state {
  margin-top: 13px;
  padding: 11px 12px;
  border-radius: 10px;
  background: rgba(0, 0, 0, 0.18);
  color: #b7ab99;
  font-size: 0.82rem;
}

.integrity-row {
  align-items: flex-start;
}

.integrity-description {
  margin-top: 8px;
  font-size: 0.82rem;
}

.integrity-description summary {
  cursor: pointer;
}

.integrity-description strong {
  display: block;
  margin-top: 8px;
}

.integrity-description p {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 20rem;
  overflow-y: auto;
}

.integrity-change-line {
  margin-top: 4px !important;
}

.warning-note {
  color: #e0b779;
}

.integrity-actions {
  display: grid;
  gap: 8px;
  margin-top: 13px;
}

.integrity-actions .primary-btn {
  margin-top: 0;
}

.restore-btn {
  width: 100%;
  padding: 10px 14px;
  border: 1px solid rgba(220, 178, 92, 0.26);
}

.record-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 11px 12px;
  border-radius: 11px;
  background: rgba(0, 0, 0, 0.18);
}

.record-row strong {
  color: #f3eadb;
  font-size: 0.9rem;
}

.status-chip {
  padding: 4px 8px;
  color: #b8d5ff;
  background: rgba(96, 143, 211, 0.16);
}

.safety-note,
.empty-copy {
  margin-top: 13px;
}

.primary-btn {
  width: 100%;
  margin-top: 13px;
  padding: 11px 14px;
  border-radius: 11px;
  font-weight: 700;
  background: linear-gradient(135deg, #3d6193, #29456d);
}

.primary-btn:disabled,
.repurchase-btn:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.buyback-row {
  align-items: stretch;
}

.record-main {
  min-width: 0;
}

.item-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
}

.quality-chip {
  padding: 3px 7px;
}

.quality-chip.exact {
  color: #aee3bd;
  background: rgba(69, 144, 91, 0.17);
}

.quality-chip.legacy {
  color: #ead29f;
  background: rgba(173, 125, 47, 0.17);
}

.price-line {
  margin-top: 5px !important;
}

.deadline-line {
  margin-top: 5px !important;
  color: #d9bd78 !important;
}

.deadline-line.expired {
  color: #e08d84 !important;
}

.price-line span {
  margin: 0 5px;
  color: #766c60;
}

.price-line strong {
  color: #f1c768;
}

.repurchase-btn {
  align-self: center;
  min-width: 82px;
  padding: 10px 13px;
  border-radius: 10px;
  background: linear-gradient(135deg, #a5762f, #745020);
  font-weight: 700;
}

@media (max-width: 640px) {
  .project-overlay {
    align-items: end;
    padding: 0;
  }

  .project-modal {
    width: 100%;
    max-height: 92vh;
    border-radius: 18px 18px 0 0;
  }

  .project-head,
  .project-body {
    padding-left: 15px;
    padding-right: 15px;
  }

  .project-title-row {
    grid-template-columns: auto minmax(0, 1fr);
  }

  .count-badge {
    grid-column: 2;
    justify-self: start;
  }

  .buyback-row {
    flex-direction: column;
  }

  .repurchase-btn {
    align-self: stretch;
  }
}
</style>
