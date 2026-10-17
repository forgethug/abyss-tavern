<template>
  <div class="gain-monitor">
    <div class="monitor-header">
      <div>
        <h6>📈 收益记录与人工查询</h6>
        <p>查看历史收益记录，或按需人工回查金币与物品账本。自建版不运行自动监控，也不会自动冻结、扣款或回收物品。</p>
      </div>
      <button type="button" class="btn-secondary btn-sm" :disabled="loading" @click="loadRecords(1)">
        {{ loading ? '刷新中…' : '刷新' }}
      </button>
    </div>

    <div class="summary-grid">
      <div class="summary-card danger">
        <span>待处理</span>
        <strong>{{ statusCount('open') }}</strong>
      </div>
      <div class="summary-card warning">
        <span>重点监控</span>
        <strong>{{ statusCount('watching') }}</strong>
      </div>
      <div class="summary-card">
        <span>已处理</span>
        <strong>{{ statusCount('resolved') }}</strong>
      </div>
      <div class="summary-card muted-card">
        <span>已排除</span>
        <strong>{{ statusCount('dismissed') }}</strong>
      </div>
    </div>

    <details class="rules-card">
      <summary>当前检测规则</summary>
      <div class="rules-grid">
        <span>单次金币 ≥ {{ formatNumber(rules.gold_single_threshold) }}</span>
        <span>每小时金币 ≥ {{ formatNumber(rules.gold_hourly_threshold) }}</span>
        <span v-for="(threshold, rarity) in rules.item_quantity_thresholds" :key="rarity">
          {{ rarityLabel(String(rarity)) }}单次 ≥ {{ threshold }} 件
        </span>
      </div>
      <p>累计规则只捕捉多笔、小额拆分增长；若单笔已触发，不再为同一小时重复生成累计告警。</p>
    </details>

    <div class="filter-grid">
      <input v-model.trim="filters.search" type="search" maxlength="100" placeholder="玩家、冒险者、物品或来源" @keyup.enter="loadRecords(1)" />
      <select v-model="filters.status">
        <option value="active">待处理与重点监控</option>
        <option value="">全部状态</option>
        <option value="open">待处理</option>
        <option value="watching">重点监控</option>
        <option value="resolved">已处理</option>
        <option value="dismissed">已排除</option>
      </select>
      <select v-model="filters.assetKind">
        <option value="">金币与物品</option>
        <option value="currency">仅金币</option>
        <option value="item">仅物品</option>
      </select>
      <select v-model="filters.severity">
        <option value="">全部级别</option>
        <option value="high">高风险</option>
        <option value="medium">需关注</option>
      </select>
      <button type="button" class="btn-primary btn-sm" :disabled="loading" @click="loadRecords(1)">查询</button>
    </div>

    <p v-if="!loading && records.length === 0" class="empty-state">暂无符合条件的异常收益记录。</p>
    <div v-else class="record-list">
      <article v-for="row in records" :key="row.id" class="record-card" :class="`severity-${row.severity}`">
        <div class="record-main">
          <div class="record-title">
            <span class="severity-badge" :class="row.severity">{{ severityLabel(row.severity) }}</span>
            <strong>{{ row.owner_nickname || row.owner_username || '无绑定玩家' }}</strong>
            <span>· {{ row.adventurer_name }} #{{ row.adventurer_id }}</span>
            <span class="status-badge" :class="row.status">{{ statusLabel(row.status) }}</span>
            <span v-if="row.is_historical" class="history-badge">历史回溯</span>
          </div>
          <div class="gain-value">
            <template v-if="row.asset_kind === 'currency'">金币 +{{ formatNumber(row.gain_amount) }}</template>
            <template v-else>「{{ row.item_name }}」+{{ formatNumber(row.gain_amount) }} 件</template>
          </div>
          <div class="source-line">
            <span>获得方式：{{ sourceLabel(row.source_type) }}</span>
            <span v-if="row.related_type || row.related_id">关联：{{ row.related_type || '记录' }} #{{ row.related_id || '-' }}</span>
            <span>发生于 {{ formatTime(row.occurred_at) }}</span>
          </div>
          <p v-if="row.description" class="description">{{ row.description }}</p>
        </div>

        <details class="evidence-details">
          <summary>查看检测证据</summary>
          <dl>
            <div><dt>命中规则</dt><dd>{{ ruleLabel(row.detector_rule) }}</dd></div>
            <div><dt>来源代码</dt><dd>{{ row.source_type }}</dd></div>
            <div v-if="row.currency_code"><dt>余额（获得后）</dt><dd>{{ formatNumber(row.balance_after) }}</dd></div>
            <div v-if="row.item_rarity"><dt>物品类型</dt><dd>{{ rarityLabel(row.item_rarity) }} · {{ row.item_type || '未分类' }}</dd></div>
            <div v-if="row.source_log_id"><dt>权威日志 ID</dt><dd>#{{ row.source_log_id }}</dd></div>
            <div v-if="row.evidence?.threshold"><dt>命中阈值</dt><dd>{{ formatNumber(row.evidence.threshold) }}</dd></div>
            <div v-if="row.evidence?.event_count"><dt>窗口笔数</dt><dd>{{ row.evidence.event_count }}</dd></div>
          </dl>
          <div v-if="sourceBreakdown(row).length" class="breakdown-list">
            <span v-for="source in sourceBreakdown(row)" :key="source.source_type">
              {{ sourceLabel(source.source_type) }}：{{ source.count }} 笔 / {{ formatNumber(source.amount) }}
            </span>
          </div>
        </details>

        <div class="review-box">
          <textarea v-model="notes[row.id]" maxlength="500" rows="2" placeholder="可选：记录核查结论或后续观察重点"></textarea>
          <div class="review-actions">
            <button type="button" class="btn-warning btn-sm" :disabled="updatingId === row.id" @click="updateStatus(row, 'watching')">重点监控</button>
            <button type="button" class="btn-primary btn-sm" :disabled="updatingId === row.id" @click="updateStatus(row, 'resolved')">标记已处理</button>
            <button type="button" class="btn-secondary btn-sm" :disabled="updatingId === row.id" @click="updateStatus(row, 'dismissed')">排除误报</button>
            <button v-if="row.status !== 'open'" type="button" class="btn-secondary btn-sm" :disabled="updatingId === row.id" @click="updateStatus(row, 'open')">重新打开</button>
          </div>
        </div>
      </article>
    </div>

    <div v-if="pages > 1" class="pager">
      <button type="button" class="btn-secondary btn-sm" :disabled="page <= 1 || loading" @click="loadRecords(page - 1)">上一页</button>
      <span>第 {{ page }} / {{ pages }} 页，共 {{ total }} 条</span>
      <button type="button" class="btn-secondary btn-sm" :disabled="page >= pages || loading" @click="loadRecords(page + 1)">下一页</button>
    </div>

    <section class="backfill-card">
      <div class="backfill-heading">
        <div>
          <h6>🕰️ 历史异常收益回溯</h6>
          <p>扫描现有金币/物品日志，先只读预览；确认后才把候选导入上方监控列表。重复执行不会产生重复记录。</p>
        </div>
      </div>
      <div class="backfill-controls">
        <label>回看天数 <input v-model.number="backfillDays" type="number" min="1" :max="rules.history_max_days || 365" /></label>
        <label>收益类型
          <select v-model="backfillAssetKind">
            <option value="all">金币与物品</option>
            <option value="currency">仅金币</option>
            <option value="item">仅物品</option>
          </select>
        </label>
        <button type="button" class="btn-secondary btn-sm" :disabled="backfillLoading" @click="previewBackfill">
          {{ backfillLoading ? '扫描中…' : '预览历史异常' }}
        </button>
      </div>

      <div v-if="backfillPreview" class="backfill-preview">
        <p>
          找到 <strong>{{ backfillPreview.candidate_count }}</strong> 条尚未导入的异常候选，
          范围 {{ formatTime(backfillPreview.start_at) }} ～ {{ formatTime(backfillPreview.end_at) }}。
        </p>
        <p v-if="backfillPreview.scan_truncated || backfillPreview.candidate_truncated" class="warning-text">
          扫描结果达到安全上限；建议缩短回看天数后分段处理。
        </p>
        <p v-if="backfillPreview.snapshot_error" class="warning-text">{{ backfillPreview.snapshot_error }}</p>
        <div class="preview-list">
          <span v-for="item in backfillPreview.items.slice(0, 20)" :key="item.event_key">
            {{ item.adventurer_name }} ·
            {{ item.asset_kind === 'currency' ? `金币 +${formatNumber(item.gain_amount)}` : `「${item.item_name}」+${formatNumber(item.gain_amount)} 件` }} ·
            {{ sourceLabel(item.source_type) }}
          </span>
        </div>
        <button
          type="button"
          class="btn-primary btn-sm"
          :disabled="backfillLoading || !backfillPreview.snapshot_token || backfillPreview.candidate_count <= 0"
          @click="executeBackfill"
        >导入 {{ backfillPreview.candidate_count }} 条监控记录</button>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { useAdventurerS1Store } from '../stores/adventurerS1'

interface SourceBreakdown {
  source_type: string
  count: number
  amount: number
}

interface GainAnomaly {
  id: number
  event_key?: string
  asset_kind: 'currency' | 'item'
  detector_rule: string
  severity: 'medium' | 'high'
  status: 'open' | 'watching' | 'resolved' | 'dismissed'
  is_historical: boolean
  adventurer_id: number
  adventurer_name: string
  owner_username?: string | null
  owner_nickname?: string | null
  source_log_id?: number | null
  currency_code?: string | null
  item_name?: string | null
  item_type?: string | null
  item_rarity?: string | null
  gain_amount: number
  balance_after?: number | null
  source_type: string
  related_id?: string | null
  related_type?: string | null
  description?: string | null
  resolution_note?: string | null
  occurred_at: string
  evidence?: {
    threshold?: number
    event_count?: number
    source_breakdown?: SourceBreakdown[]
  }
}

interface MonitorRules {
  gold_single_threshold?: number
  gold_hourly_threshold?: number
  item_quantity_thresholds?: Record<string, number>
  history_max_days?: number
}

interface BackfillPreview {
  candidate_count: number
  start_at: string
  end_at: string
  scan_truncated?: boolean
  candidate_truncated?: boolean
  snapshot_token?: string | null
  snapshot_error?: string
  items: GainAnomaly[]
}

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const loading = ref(false)
const records = ref<GainAnomaly[]>([])
const page = ref(1)
const pages = ref(1)
const total = ref(0)
const statusCounts = ref<Record<string, number>>({})
const rules = ref<MonitorRules>({ item_quantity_thresholds: {} })
const updatingId = ref<number | null>(null)
const notes = reactive<Record<number, string>>({})
const filters = reactive({ status: 'active', assetKind: '', severity: '', search: '' })
const backfillDays = ref(30)
const backfillAssetKind = ref('all')
const backfillLoading = ref(false)
const backfillPreview = ref<BackfillPreview | null>(null)

const SOURCE_LABELS: Record<string, string> = {
  expedition_reward: '探险结算',
  expedition: '探险获得',
  task_reward: '任务奖励',
  commission_reward: '委托奖励',
  building: '建筑产出',
  market_buy: '市场买入',
  market_trade: '市场交易',
  gambling_payout: '赌坊派彩',
  lucky_wheel: '幸运大转盘',
  data_appeal_ai: '数据申诉补发',
  admin_adjust: '管理员调整',
  multiple_sources: '多种来源累计',
}

function statusCount(status: string) {
  return statusCounts.value[status] || 0
}

function formatNumber(value: unknown) {
  const number = Number(value || 0)
  return Number.isInteger(number) ? number.toLocaleString() : number.toLocaleString(undefined, { maximumFractionDigits: 2 })
}

function formatTime(value?: string | null) {
  if (!value) return '-'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function sourceLabel(source: string) {
  const label = SOURCE_LABELS[source]
  return label ? `${label}（${source}）` : source || '未知来源'
}

function statusLabel(status: string) {
  return ({ open: '待处理', watching: '重点监控', resolved: '已处理', dismissed: '已排除' } as Record<string, string>)[status] || status
}

function severityLabel(severity: string) {
  return severity === 'high' ? '高风险' : '需关注'
}

function rarityLabel(rarity: string) {
  return ({ common: '普通', uncommon: '优秀', rare: '稀有', epic: '史诗', legendary: '传说' } as Record<string, string>)[rarity] || rarity
}

function ruleLabel(rule: string) {
  return ({
    gold_single_gain: '单次金币异常增长',
    gold_hourly_gain: '小时金币累计异常增长',
    item_single_gain: '单次物品数量异常增长',
    item_hourly_gain: '小时同名物品累计异常增长',
  } as Record<string, string>)[rule] || rule
}

function sourceBreakdown(row: GainAnomaly) {
  return Array.isArray(row.evidence?.source_breakdown) ? row.evidence.source_breakdown : []
}

async function loadRecords(targetPage = page.value) {
  loading.value = true
  try {
    const res = await store.adminListGainAnomalies({
      page: targetPage,
      per_page: 20,
      status: filters.status || undefined,
      asset_kind: filters.assetKind || undefined,
      severity: filters.severity || undefined,
      search: filters.search || undefined,
    })
    if (res.code !== 200) {
      message.error(res.message || '加载收益异常监控失败')
      return
    }
    const data = res.data || {}
    records.value = data.items || []
    page.value = data.page || 1
    pages.value = data.pages || 1
    total.value = data.total || 0
    statusCounts.value = data.summary?.status_counts || {}
    rules.value = data.summary?.rules || { item_quantity_thresholds: {} }
    for (const row of records.value) notes[row.id] = row.resolution_note || notes[row.id] || ''
  } catch (error: any) {
    message.error(error?.response?.data?.message || '加载收益异常监控失败')
  } finally {
    loading.value = false
  }
}

async function updateStatus(row: GainAnomaly, status: string) {
  updatingId.value = row.id
  try {
    const res = await store.adminUpdateGainAnomaly(row.id, status, notes[row.id] || '')
    if (res.code !== 200) {
      message.error(res.message || '更新监控状态失败')
      return
    }
    message.success(`已将记录标记为“${statusLabel(status)}”`)
    await loadRecords(page.value)
  } catch (error: any) {
    message.error(error?.response?.data?.message || '更新监控状态失败')
  } finally {
    updatingId.value = null
  }
}

async function previewBackfill() {
  backfillLoading.value = true
  backfillPreview.value = null
  try {
    const res = await store.adminPreviewGainAnomalyBackfill(backfillDays.value, backfillAssetKind.value)
    if (res.code !== 200) {
      message.error(res.message || '历史回溯预览失败')
      return
    }
    backfillPreview.value = res.data
    message.success(`历史扫描完成，找到 ${res.data?.candidate_count || 0} 条候选`)
  } catch (error: any) {
    message.error(error?.response?.data?.message || '历史回溯预览失败')
  } finally {
    backfillLoading.value = false
  }
}

async function executeBackfill() {
  const preview = backfillPreview.value
  if (!preview?.snapshot_token) return
  if (!window.confirm(`确认把预览中的 ${preview.candidate_count} 条历史候选导入监控列表？此操作不会修改玩家资产。`)) return
  backfillLoading.value = true
  try {
    const res = await store.adminExecuteGainAnomalyBackfill(preview.snapshot_token)
    if (res.code !== 200) {
      message.error(res.message || '历史监控记录导入失败')
      return
    }
    message.success(`已导入 ${res.data?.created_count || 0} 条历史监控记录`)
    backfillPreview.value = null
    await loadRecords(1)
  } catch (error: any) {
    message.error(error?.response?.data?.message || '历史监控记录导入失败')
  } finally {
    backfillLoading.value = false
  }
}

onMounted(() => loadRecords(1))
</script>

<style scoped>
.gain-monitor { display: grid; gap: 14px; min-width: 0; }
.monitor-header, .backfill-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; min-width: 0; }
.monitor-header > div, .backfill-heading > div { min-width: 0; }
.monitor-header h6, .backfill-heading h6 { margin: 0 0 4px; }
.monitor-header p, .backfill-heading p, .rules-card p { margin: 0; color: var(--text-secondary, #64748b); font-size: 13px; line-height: 1.6; }
.summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.summary-card { padding: 12px; border: 1px solid var(--border-color, #d8dee9); border-radius: 10px; background: var(--card-bg, #fff); display: flex; justify-content: space-between; align-items: center; }
.summary-card strong { font-size: 22px; }
.summary-card.danger strong { color: #b91c1c; }
.summary-card.warning strong { color: #b45309; }
.summary-card.muted-card strong { color: #64748b; }
.rules-card, .backfill-card { border: 1px solid var(--border-color, #d8dee9); border-radius: 10px; padding: 12px; background: var(--card-bg, #fff); }
.rules-card summary, .evidence-details summary { cursor: pointer; font-weight: 600; }
.rules-grid { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 6px; }
.rules-grid span { padding: 4px 8px; border-radius: 999px; background: var(--subtle-bg, #f1f5f9); font-size: 12px; }
.filter-grid { display: grid; grid-template-columns: minmax(180px, 1.7fr) repeat(3, minmax(110px, 1fr)) auto; gap: 8px; }
.filter-grid input, .filter-grid select, .backfill-controls input, .backfill-controls select, .review-box textarea { width: 100%; min-width: 0; border: 1px solid var(--border-color, #cbd5e1); border-radius: 8px; padding: 8px 10px; background: var(--input-bg, #fff); color: var(--text-color, #1f2937); }
.record-list { display: grid; gap: 10px; min-width: 0; }
.record-card { min-width: 0; border: 1px solid var(--border-color, #d8dee9); border-left: 4px solid #d97706; border-radius: 10px; padding: 12px; background: var(--card-bg, #fff); }
.record-card.severity-high { border-left-color: #dc2626; }
.record-title { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; min-width: 0; }
.severity-badge, .status-badge, .history-badge { padding: 2px 7px; border-radius: 999px; font-size: 12px; white-space: nowrap; }
.severity-badge.high { color: #991b1b; background: #fee2e2; }
.severity-badge.medium { color: #92400e; background: #fef3c7; }
.status-badge.open { color: #991b1b; background: #fee2e2; }
.status-badge.watching { color: #92400e; background: #fef3c7; }
.status-badge.resolved { color: #166534; background: #dcfce7; }
.status-badge.dismissed { color: #475569; background: #e2e8f0; }
.history-badge { color: #5b21b6; background: #ede9fe; }
.gain-value { margin-top: 8px; font-size: 18px; font-weight: 700; overflow-wrap: anywhere; }
.source-line { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 6px; color: var(--text-secondary, #64748b); font-size: 13px; overflow-wrap: anywhere; }
.description { margin: 6px 0 0; overflow-wrap: anywhere; }
.evidence-details { margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--border-color, #d8dee9); }
.evidence-details dl { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 10px 0; }
.evidence-details dl div { min-width: 0; }
.evidence-details dt { color: var(--text-secondary, #64748b); font-size: 12px; }
.evidence-details dd { margin: 2px 0 0; overflow-wrap: anywhere; }
.breakdown-list, .preview-list { display: grid; gap: 5px; min-width: 0; font-size: 13px; }
.breakdown-list span, .preview-list span { min-width: 0; overflow-wrap: anywhere; }
.review-box { display: grid; grid-template-columns: minmax(180px, 1fr) auto; gap: 8px; align-items: end; margin-top: 10px; }
.review-box textarea { resize: vertical; }
.review-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.pager { display: flex; align-items: center; justify-content: center; gap: 12px; }
.backfill-card { display: grid; gap: 12px; border-color: #a78bfa; }
.backfill-controls { display: flex; align-items: end; flex-wrap: wrap; gap: 10px; }
.backfill-controls label { display: grid; gap: 4px; min-width: 140px; font-size: 13px; }
.backfill-preview { display: grid; gap: 8px; padding: 10px; border-radius: 8px; background: var(--subtle-bg, #f8fafc); }
.backfill-preview p { margin: 0; }
.warning-text { color: #b45309; }
.empty-state { padding: 20px; text-align: center; color: var(--text-secondary, #64748b); }
@media (max-width: 800px) {
  .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .filter-grid { grid-template-columns: 1fr 1fr; }
  .filter-grid input { grid-column: 1 / -1; }
  .review-box { grid-template-columns: 1fr; }
  .review-actions { justify-content: flex-start; }
  .evidence-details dl { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 520px) {
  .monitor-header { flex-direction: column; }
  .monitor-header > button { width: 100%; min-height: 40px; }
  .filter-grid, .evidence-details dl { grid-template-columns: 1fr; }
  .filter-grid input { grid-column: auto; }
  .filter-grid button, .review-actions button, .backfill-controls button { width: 100%; min-height: 40px; }
  .review-actions { display: grid; grid-template-columns: 1fr; }
  .backfill-controls { display: grid; grid-template-columns: 1fr; align-items: stretch; }
  .backfill-controls label { min-width: 0; }
  .pager { display: grid; grid-template-columns: 1fr; text-align: center; }
}
</style>
