<template>
  <section class="admin-secret-auction">
    <div class="panel-header">
      <div>
        <h3 class="col-title">🎭 {{ isReadonly ? '隐秘拍卖行' : '隐秘拍卖行管理' }}</h3>
        <p>{{ isReadonly ? '查看每件拍品的竞拍与结算状态；进行中的出价金额会在拍卖结束后显示。' : '上架新拍品，并独立查看每件拍品的竞拍与结算状态。' }}</p>
      </div>
    </div>

    <section v-if="!isReadonly" class="listing-entry" aria-labelledby="listing-entry-title">
      <div class="listing-entry-icon" aria-hidden="true">◇</div>
      <div class="listing-entry-copy">
        <span class="section-kicker">NEW LISTING</span>
        <h4 id="listing-entry-title">新拍品上架</h4>
        <p>一次录入多件拍品，统一设置开奖时间与默认底价，也可为单件拍品配置独立规则。</p>
        <div class="listing-features" aria-label="批量上架功能">
          <span>图鉴快速带入</span>
          <span>批量统一设置</span>
          <span>单品规则覆盖</span>
        </div>
      </div>
      <button type="button" class="advs1-submit listing-entry-action" @click="openCreate">
        <span>＋</span> 批量上架拍品
      </button>
    </section>

    <section class="status-workspace" aria-labelledby="status-workspace-title">
      <div class="workspace-heading">
        <div>
          <span class="section-kicker">AUCTION STATUS</span>
          <h4 id="status-workspace-title">拍品状态</h4>
          <p>{{ isReadonly ? '筛选拍品并查看竞拍状态；进行中的密封出价金额暂不公开。' : '筛选拍品、查看出价明细，并处理仍在进行中的拍卖。' }}</p>
        </div>
        <button v-if="!isReadonly" type="button" class="btn-secondary btn-sm" :disabled="fixBusy" @click="previewFix">
          🔧 修复残留出价
        </button>
      </div>

      <div class="auction-overview" aria-label="拍卖概览">
        <div class="overview-card overview-card--primary">
          <span class="overview-icon">◈</span>
          <div>
            <span class="overview-label">拍品总数</span>
            <strong>{{ pagination?.total ?? lots.length }}</strong>
          </div>
        </div>
        <div class="overview-card overview-card--success">
          <span class="overview-icon">●</span>
          <div>
            <span class="overview-label">本页进行中</span>
            <strong>{{ pageOpenCount }}</strong>
          </div>
        </div>
        <div class="overview-card overview-card--warning">
          <span class="overview-icon">◆</span>
          <div>
            <span class="overview-label">本页总出价</span>
            <strong>{{ pageBidCount.toLocaleString() }}</strong>
          </div>
        </div>
        <div class="overview-card overview-card--gold">
          <span class="overview-icon">¤</span>
          <div>
            <span class="overview-label">本页成交额</span>
            <strong>{{ pageTurnover.toFixed(2) }}</strong>
          </div>
        </div>
      </div>

      <div class="filter-row">
        <span class="filter-label">状态筛选</span>
        <button
          v-for="f in STATUS_FILTERS"
          :key="f.value"
          type="button"
          class="chip"
          :class="{ active: statusFilter === f.value }"
          @click="setStatusFilter(f.value)"
        >{{ f.label }}</button>
        <button type="button" class="btn-secondary btn-sm refresh-button" :disabled="loading" @click="loadLots()">
          <span :class="{ spinning: loading }">↻</span> 刷新
        </button>
      </div>

      <div v-if="listError" class="advs1-state error">{{ listError }}</div>
      <div v-else-if="loading && lots.length === 0" class="advs1-state">加载中…</div>
      <div v-else-if="lots.length === 0" class="advs1-state">暂无拍品</div>
      <div v-else class="lot-list">
        <div v-for="lot in lots" :key="lot.id" class="lot-row">
          <div class="lot-main">
            <strong :style="{ color: rarityColor(lot.rarity) }">{{ lot.item_name }}</strong>
            <span v-if="lot.item_type" class="meta">{{ itemTypeLabel(lot.item_type) }}</span>
            <span class="status-tag" :class="`status-tag--${lot.status}`">{{ statusLabel(lot.status) }}</span>
            <span class="meta">👥 {{ lot.bid_count ?? 0 }} 人</span>
            <span class="meta">⏰ {{ lot.ends_at ? formatDateTime(lot.ends_at) : '—' }}</span>
            <span v-if="(lot.reserve_price ?? 0) > 0" class="meta">📌 底价 {{ lot.reserve_price!.toFixed(2) }}💰</span>
            <span v-if="lot.uses_left != null" class="meta">🔢 {{ lot.uses_left }} 次</span>
            <span v-if="canShowWinningAmount(lot)" class="meta">成交价 {{ lot.winning_amount }}💰</span>
          </div>
          <div v-if="lot.bids?.length" class="bid-detail">
            <span v-if="!canShowBidAmount(lot)" class="bid-chip bid-chip--hidden">
              🔒 出价金额将在拍卖结束后显示
            </span>
            <span v-for="b in lot.bids" :key="b.id" class="bid-chip">
              {{ b.adventurer_name }}
              <template v-if="canShowBidAmount(lot) && b.amount != null">: {{ b.amount }}💰</template>
              ({{ b.status }})
            </span>
          </div>
          <div v-if="!isReadonly" class="lot-actions">
            <button
              v-if="lot.status === 'open'"
              type="button"
              class="btn-reject btn-sm"
              :disabled="busy"
              @click="cancelLot(lot.id)"
            >取消并退钱</button>
          </div>
        </div>
      </div>
      <Pagination v-if="pagination" :pagination="pagination" @go="goPage" />
    </section>

    <!-- 修复残留出价弹窗 -->
    <div v-if="showFix && !isReadonly" class="modal-mask s1-mobile-surface s1-mobile-overlay" @click.self="showFix = false">
      <div class="modal-box modal-form s1-mobile-dialog">
        <h4 class="modal-title">🔧 修复残留出价</h4>
        <p class="form-hint">因改价导致 escrow 归零、结算时未被处理的 held bid。预览后确认执行。</p>
        <div v-if="fixBusy" class="advs1-state">加载中…</div>
        <div v-else-if="fixPreview">
          <div v-if="fixPreview.affected.length === 0" class="advs1-state">✅ 无残留出价，无需修复</div>
          <div v-else>
            <p class="fix-summary">
              共 <strong>{{ fixPreview.affected.length }}</strong> 条残留，
              涉及出价 <strong>{{ fixPreview.total_gold }}💰</strong>
            </p>
            <div class="fix-list">
              <div v-for="item in fixPreview.affected" :key="`${item.lot_id}-${item.adventurer_id}`" class="fix-row">
                <span class="fix-lot">拍品#{{ item.lot_id }} {{ item.item_name }}</span>
                <span class="fix-adv">{{ item.adventurer_name }}(#{{ item.adventurer_id }})</span>
                <span class="fix-amount">{{ item.bid_amount }}💰</span>
              </div>
            </div>
            <p class="form-hint fix-note">执行后将这些 bid 标记为 refunded（金币无需退还，改价时已退回）。</p>
          </div>
        </div>
        <div class="form-actions s1-mobile-dialog-footer">
          <button type="button" class="btn-secondary" @click="showFix = false">关闭</button>
          <button
            v-if="fixPreview && fixPreview.affected.length > 0"
            type="button"
            class="advs1-submit"
            :disabled="fixBusy"
            @click="applyFix"
          >确认修复</button>
        </div>
      </div>
    </div>

    <!-- 批量上架弹窗 -->
    <div v-if="showCreate && !isReadonly" class="modal-mask s1-mobile-surface s1-mobile-overlay" @click.self="showCreate = false">
      <div class="modal-box modal-form create-modal s1-mobile-dialog">
        <div class="create-heading s1-mobile-dialog-header">
          <div class="create-heading-icon">◇</div>
          <div>
            <h4 class="modal-title">批量上架拍品</h4>
            <p class="form-hint">从图鉴快速带入物品资料，或直接创建新的自定义拍品。</p>
          </div>
          <span class="lot-count-badge">{{ form.lots.length }} 件拍品</span>
          <button type="button" class="modal-close" aria-label="关闭批量上架" @click="showCreate = false">×</button>
        </div>

        <section class="create-section batch-settings">
          <div class="section-heading">
            <div>
              <strong>统一拍卖设置</strong>
              <span>应用于本批次所有拍品</span>
            </div>
          </div>
          <div class="batch-fields">
            <label class="form-label">
              <span>开奖时间 <em>必填</em></span>
              <input v-model="form.ends_at" type="datetime-local" class="advs1-input" />
            </label>
            <label class="form-label">
              <span>默认底价 <small>0 或留空表示无底价</small></span>
              <div class="input-with-suffix">
                <input v-model.number="form.reserve_price" type="number" min="0" step="0.01" class="advs1-input" placeholder="0.00" />
                <span>💰</span>
              </div>
            </label>
          </div>
        </section>

        <div class="items-section-heading s1-mobile-dialog-header">
          <div>
            <strong>拍品清单</strong>
            <span>单项底价会覆盖上方的默认底价</span>
          </div>
          <button type="button" class="btn-secondary btn-sm" @click="addRow">＋ 添加拍品</button>
        </div>
        <div v-for="(row, idx) in form.lots" :key="idx" class="lot-form-row">
          <div class="row-head">
            <div class="lot-index"><span>{{ idx + 1 }}</span> 拍品资料</div>
            <button v-if="form.lots.length > 1" type="button" class="btn-link remove-lot" @click="removeRow(idx)">移除</button>
          </div>
          <div class="primary-fields">
            <label class="field-group catalog-field">
              <span class="field-label">物品名称 <em>必填 · 支持搜索图鉴</em></span>
              <input
                v-model="row.item_name"
                class="advs1-input"
                placeholder="输入名称搜索，或创建新物品"
                @input="onCatalogInput(idx)"
                @focus="activeCatalogRow = idx"
              />
              <div v-if="activeCatalogRow === idx && catalogResults.length > 0" class="catalog-dropdown">
                <div class="catalog-dropdown-title">图鉴匹配结果</div>
                <button
                  v-for="c in catalogResults"
                  :key="c.id"
                  type="button"
                  class="catalog-item"
                  @click="pickCatalogItem(idx, c)"
                >
                  <span class="catalog-name">{{ c.name }}</span>
                  <span class="catalog-rarity">{{ rarityLabel(c.rarity) }}</span>
                  <span class="catalog-type">{{ itemTypeLabel(c.type) }}</span>
                  <span v-if="catalogItemIsImmutable(c)" class="catalog-rule catalog-rule--immutable">不可变</span>
                  <span v-if="catalogItemDungeon(c)" class="catalog-rule">
                    {{ catalogItemDungeon(c) }}{{ catalogItemTargetClass(c) ? ` · ${catalogItemTargetClass(c)}` : '' }}
                  </span>
                </button>
              </div>
            </label>
            <label class="field-group">
              <span class="field-label">物品描述 <em>可选</em></span>
              <input v-model="row.custom_desc" class="advs1-input" placeholder="补充用途、背景或特殊说明" />
            </label>
          </div>
          <div class="form-block">
            <span class="form-block-title">基础属性</span>
            <div class="row-inline">
              <label class="field-group">
                <span class="field-label">类型</span>
                <select v-model="row.item_type" class="advs1-input">
                  <option v-for="t in ITEM_TYPE_OPTIONS" :key="t.value" :value="t.value">{{ t.label }}</option>
                </select>
              </label>
              <label class="field-group">
                <span class="field-label">稀有度</span>
                <select v-model="row.rarity" class="advs1-input">
                  <option value="common">普通</option>
                  <option value="rare">稀有</option>
                  <option value="epic">史诗</option>
                  <option value="legendary">传说</option>
                </select>
              </label>
              <label class="field-group">
                <span class="field-label">数量</span>
                <input v-model.number="row.quantity" type="number" min="1" class="advs1-input" placeholder="1" />
              </label>
              <AdminItemUsesLeftField
                v-model="row.uses_left"
                label="使用次数"
                placeholder="不限"
                hint=""
                class="uses-left-inline"
              />
            </div>
          </div>
          <div class="auction-rules-grid">
            <div class="pricing-panel">
              <div>
                <strong>单项底价</strong>
                <span>留空时沿用本批次默认底价</span>
              </div>
              <div class="input-with-suffix">
                <input v-model.number="row.reserve_price" type="number" min="0" step="0.01" class="advs1-input" placeholder="使用默认底价" />
                <span>💰</span>
              </div>
            </div>
            <div class="condition-panel" :class="{ active: row.text_locked || row.uc_enabled }">
              <label class="uc-toggle">
                <input v-model="row.text_locked" type="checkbox" :disabled="row.catalog_item_id != null" />
                <span class="toggle-box"></span>
                <span><strong>不可变</strong><small>禁止申诉、炼金与嬗变修改；正常用途仍可消耗</small></span>
              </label>
              <div class="condition-divider"></div>
              <label class="uc-toggle">
                <input v-model="row.uc_enabled" type="checkbox" :disabled="row.catalog_item_id != null" />
                <span class="toggle-box"></span>
                <span><strong>限制使用场景</strong><small>仅允许在指定副本或职业条件下使用</small></span>
              </label>
              <div v-if="row.uc_enabled" class="uc-fields">
                <label class="field-group">
                  <span class="field-label">指定副本 <em>必填</em></span>
                  <select v-model="row.uc_dungeon" class="advs1-input" :disabled="row.catalog_item_id != null">
                    <option value="">请选择副本</option>
                    <option v-for="d in dungeonList" :key="d" :value="d">{{ d }}</option>
                  </select>
                </label>
                <label class="field-group">
                  <span class="field-label">目标职业 <em>可选</em></span>
                  <input v-model="row.uc_target_class" class="advs1-input" placeholder="不填则不限职业" maxlength="20" :disabled="row.catalog_item_id != null" />
                </label>
              </div>
              <p v-if="row.catalog_item_id != null" class="catalog-source-note">
                已从图鉴带入不可变与使用限制；修改请前往物品图鉴。
              </p>
            </div>
          </div>
        </div>
        <button type="button" class="add-lot-button" @click="addRow">＋ 添加另一件拍品</button>
        <div class="form-actions create-actions s1-mobile-dialog-footer">
          <span>即将上架 <strong>{{ form.lots.filter(row => row.item_name.trim()).length }}</strong> 件有效拍品</span>
          <button type="button" class="btn-secondary" @click="showCreate = false">取消</button>
          <button type="button" class="advs1-submit" :disabled="busy" @click="submitCreate">
            {{ busy ? '提交中…' : `确认上架 ${form.lots.filter(row => row.item_name.trim()).length} 件` }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, watch } from 'vue'
import { useAdventurerS1Store, type SecretAuctionLotAdmin, type PaginationMeta } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import Pagination from '@/components/Pagination.vue'
import { formatDateTime } from '@/utils/time'
import { rarityColor, rarityLabel, itemTypeLabel, ITEM_TYPE_OPTIONS, S1_DEFAULT_ITEM_TYPE } from '../utils'
import {
  extractDefaultUsesLeft,
  parseCatalogUseConditionToForm,
  type CatalogUseConditionFormSlice,
} from '../composables/adminItemForm'
import AdminItemUsesLeftField from './AdminItemUsesLeftField.vue'

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const props = withDefaults(defineProps<{
  readonly?: boolean
}>(), {
  readonly: false,
})
const isReadonly = computed(() => props.readonly)

const STATUS_FILTERS = [
  { value: '', label: '全部' },
  { value: 'open', label: '进行中' },
  { value: 'settled_won', label: '已成交' },
  { value: 'settled_pass', label: '流拍' },
  { value: 'cancelled', label: '已取消' },
]
const TERMINAL_AUCTION_STATUSES = new Set(['settled_won', 'settled_pass', 'cancelled'])

const loading = ref(false)
const busy = ref(false)
const listError = ref('')
const lots = ref<SecretAuctionLotAdmin[]>([])
const pagination = ref<PaginationMeta | null>(null)
const statusFilter = ref('')
const showCreate = ref(false)
const catalogResults = ref<CatalogItemOption[]>([])
const activeCatalogRow = ref<number | null>(null)
const dungeonList = ref<string[]>([])
const pageOpenCount = computed(() => lots.value.filter(lot => lot.status === 'open').length)
const pageBidCount = computed(() => lots.value.reduce((sum, lot) => sum + (lot.bid_count ?? 0), 0))
const pageTurnover = computed(() => lots.value.reduce(
  (sum, lot) => sum + (canShowWinningAmount(lot) ? (lot.winning_amount ?? 0) : 0),
  0,
))

const showFix = ref(false)
const fixBusy = ref(false)
const fixPreview = ref<{ affected: { lot_id: number; item_name: string; adventurer_id: number; adventurer_name: string; bid_amount: number }[]; total_gold: number } | null>(null)

function buildRowUseCondition(row: LotFormRow): Record<string, unknown> | undefined {
  const cond: Record<string, unknown> = {}
  if (row.text_locked) cond.immutable = true
  if (row.uc_enabled) {
    const dungeon = row.uc_dungeon.trim()
    if (dungeon) {
      cond.expedition_dungeon = dungeon
      const targetClass = row.uc_target_class.trim()
      if (targetClass) cond.target_class = targetClass
    }
  }
  return Object.keys(cond).length ? cond : undefined
}
let catalogTimer: ReturnType<typeof setTimeout> | null = null

interface CatalogItemOption {
  id: number
  name: string
  type: string
  rarity: string
  description?: string
  text_locked?: boolean
  default_uses_left?: number | null
  use_condition?: Record<string, unknown> | null
}

interface LotFormRow extends CatalogUseConditionFormSlice {
  catalog_item_id: number | null
  item_name: string
  item_type: string
  custom_desc: string
  rarity: string
  quantity: number
  reserve_price: number | null
}

const emptyRow = (): LotFormRow => ({
  catalog_item_id: null,
  item_name: '',
  item_type: S1_DEFAULT_ITEM_TYPE,
  custom_desc: '',
  rarity: 'rare',
  quantity: 1,
  uses_left: null,
  reserve_price: null,
  uc_enabled: false,
  uc_dungeon: '',
  uc_target_class: '',
  text_locked: false,
})

const form = ref<{ ends_at: string; reserve_price: number | null; lots: LotFormRow[] }>({
  ends_at: '',
  reserve_price: null,
  lots: [emptyRow()],
})

function statusLabel(s: string): string {
  return {
    open: '进行中',
    settled_won: '已成交',
    settled_pass: '流拍',
    cancelled: '已取消',
  }[s] ?? s
}

function canShowAuctionPrices(lot: SecretAuctionLotAdmin): boolean {
  return !isReadonly.value || TERMINAL_AUCTION_STATUSES.has(lot.status)
}

function canShowWinningAmount(lot: SecretAuctionLotAdmin): boolean {
  return lot.winning_amount != null && canShowAuctionPrices(lot)
}

function canShowBidAmount(lot: SecretAuctionLotAdmin): boolean {
  return !lot.bid_amounts_hidden && canShowAuctionPrices(lot)
}

function setStatusFilter(v: string) {
  statusFilter.value = v
  void loadLots(1)
}

function goPage(page: number) {
  void loadLots(page)
}

async function loadLots(page = 1) {
  loading.value = true
  listError.value = ''
  try {
    const res = await store.fetchAdminSecretAuctionLots(page, statusFilter.value || undefined)
    if (res.code === 200 && res.data) {
      lots.value = res.data.items ?? []
      pagination.value = {
        current_page: res.data.page ?? page,
        pages: res.data.pages ?? 1,
        total: res.data.total ?? 0,
      }
    } else {
      lots.value = []
      pagination.value = null
      listError.value = res.message || '加载拍品列表失败'
      message.error(listError.value)
    }
  } catch (e: any) {
    lots.value = []
    pagination.value = null
    listError.value = e?.response?.data?.message || '加载失败'
    message.error(listError.value)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  form.value = { ends_at: '', reserve_price: null, lots: [emptyRow()] }
  catalogResults.value = []
  activeCatalogRow.value = null
  showCreate.value = true
}

function addRow() {
  form.value.lots.push(emptyRow())
}

function removeRow(idx: number) {
  form.value.lots.splice(idx, 1)
}

function onCatalogInput(idx: number) {
  activeCatalogRow.value = idx
  if (catalogTimer) clearTimeout(catalogTimer)
  const row = form.value.lots[idx]
  if (row.catalog_item_id != null) {
    row.catalog_item_id = null
    row.text_locked = false
    row.uc_enabled = false
    row.uc_dungeon = ''
    row.uc_target_class = ''
    row.uses_left = null
  }
  const q = row.item_name.trim()
  if (q.length < 1) {
    catalogResults.value = []
    return
  }
  catalogTimer = setTimeout(async () => {
    try {
      const res = await store.adminSearchItemCatalog({ search: q })
      if (res.code === 200) catalogResults.value = res.data ?? []
    } catch {
      catalogResults.value = []
    }
  }, 300)
}

function catalogItemIsImmutable(c: CatalogItemOption): boolean {
  return c.text_locked === true
    || c.use_condition?.immutable === true
    || c.use_condition?.text_locked === true
}

function catalogItemDungeon(c: CatalogItemOption): string {
  return typeof c.use_condition?.expedition_dungeon === 'string'
    ? c.use_condition.expedition_dungeon
    : ''
}

function catalogItemTargetClass(c: CatalogItemOption): string {
  return typeof c.use_condition?.target_class === 'string'
    ? c.use_condition.target_class
    : ''
}

function pickCatalogItem(idx: number, c: CatalogItemOption) {
  const row = form.value.lots[idx]
  row.catalog_item_id = c.id
  row.item_name = c.name
  row.item_type = c.type || S1_DEFAULT_ITEM_TYPE
  row.rarity = c.rarity || 'common'
  row.custom_desc = c.description || ''
  parseCatalogUseConditionToForm(c.use_condition, row)
  row.text_locked = catalogItemIsImmutable(c)
  row.uses_left = extractDefaultUsesLeft(c.use_condition, c.default_uses_left ?? null)
  catalogResults.value = []
  activeCatalogRow.value = null
}

function toIsoEndsAt(local: string): string {
  if (!local) return ''
  return new Date(local).toISOString()
}

async function submitCreate() {
  if (busy.value) return
  const validLots = form.value.lots.filter(r => r.item_name.trim())
  if (validLots.length === 0) {
    message.warning('请至少填写一个物品名称')
    return
  }
  if (!form.value.ends_at) {
    message.warning('请设置开奖时间')
    return
  }
  busy.value = true
  try {
    const defaultReserve = form.value.reserve_price != null && form.value.reserve_price > 0
      ? form.value.reserve_price
      : undefined
    const res = await store.adminCreateSecretAuctionLots(
      toIsoEndsAt(form.value.ends_at),
      validLots.map(r => {
        const useCondition = buildRowUseCondition(r)
        return {
          item_name: r.item_name.trim(),
          item_type: r.item_type,
          rarity: r.rarity,
          custom_desc: r.custom_desc.trim() || undefined,
          quantity: r.quantity || 1,
          ...(r.uses_left != null && r.uses_left >= 0 ? { uses_left: r.uses_left } : {}),
          ...(r.reserve_price != null && r.reserve_price > 0 ? { reserve_price: r.reserve_price } : {}),
          ...(useCondition ? { use_condition: useCondition } : {}),
        }
      }),
      defaultReserve,
    )
    if (res.code === 201 || res.code === 200) {
      message.success(`已上架 ${res.data?.created_count ?? validLots.length} 个拍品`)
      showCreate.value = false
      statusFilter.value = ''
      await loadLots(1)
    } else {
      message.error(res.message || '上架失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '上架失败')
  } finally {
    busy.value = false
  }
}

async function cancelLot(lotId: number) {
  if (busy.value) return
  busy.value = true
  try {
    const res = await store.adminCancelSecretAuctionLot(lotId)
    if (res.code === 200) {
      message.success('已取消，出价金币已退还')
      await loadLots(pagination.value?.current_page ?? 1)
    } else {
      message.error(res.message || '取消失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '取消失败')
  } finally {
    busy.value = false
  }
}

async function previewFix() {
  showFix.value = true
  fixPreview.value = null
  fixBusy.value = true
  try {
    const res = await store.adminPreviewUnfundedFix()
    if (res.code === 200) {
      fixPreview.value = res.data
    } else {
      message.error(res.message || '预览失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '预览失败')
  } finally {
    fixBusy.value = false
  }
}

async function applyFix() {
  if (fixBusy.value) return
  fixBusy.value = true
  try {
    const res = await store.adminApplyUnfundedFix()
    if (res.code === 200) {
      message.success(`已修复 ${res.data?.fixed ?? 0} 条残留出价`)
      showFix.value = false
      await loadLots(pagination.value?.current_page ?? 1)
    } else {
      message.error(res.message || '修复失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '修复失败')
  } finally {
    fixBusy.value = false
  }
}

onMounted(() => {
  void loadLots(1)
  if (!isReadonly.value) {
    void store.adminGetDungeonList().then(list => { dungeonList.value = list })
  }
})

watch(isReadonly, readonly => {
  showCreate.value = false
  showFix.value = false
  fixPreview.value = null
  lots.value = []
  pagination.value = null
  void loadLots(1)
  if (!readonly) {
    void store.adminGetDungeonList().then(list => { dungeonList.value = list })
  }
})
</script>

<style scoped lang="scss">
.admin-secret-auction {
  margin-top: 16px;
  min-width: 0;
  max-width: 100%;
  box-sizing: border-box;
  overflow-wrap: anywhere;
}

.modal-mask {
  position: fixed;
  z-index: 1200;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 16px;
  overflow-y: auto;
  background: rgba(15, 23, 42, 0.58);
  backdrop-filter: blur(2px);
}

.modal-box {
  width: min(560px, calc(100vw - 32px));
  min-width: 0;
  max-width: 100%;
  max-height: calc(100dvh - 32px);
  box-sizing: border-box;
  overflow-y: auto;
  border: 1px solid var(--color-border);
  border-radius: 14px;
  background: var(--color-bg, #fff);
  color: var(--color-text);
  box-shadow: 0 24px 70px rgba(15, 23, 42, 0.28);
}

.modal-form:not(.create-modal) { padding: 18px; }

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 18px;
  gap: 16px;
  min-width: 0;

  .col-title { margin-bottom: 4px; }
  p { margin: 0; color: var(--color-text-secondary); font-size: 12px; }
}

.listing-entry {
  position: relative;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 18px;
  margin-bottom: 22px;
  overflow: hidden;
  padding: 20px 22px;
  border: 1px solid rgba(124, 92, 231, 0.28);
  border-radius: 14px;
  background: var(--color-bg, #fff);
  background: linear-gradient(125deg, color-mix(in srgb, var(--color-primary, #7c5ce7) 12%, var(--color-bg, #fff)), var(--color-bg, #fff) 64%);
  box-shadow: 0 9px 26px rgba(67, 44, 125, 0.09);

  &::after {
    position: absolute;
    top: -70px;
    right: 12%;
    width: 180px;
    height: 180px;
    border: 1px solid rgba(124, 92, 231, 0.12);
    border-radius: 50%;
    content: '';
    pointer-events: none;
  }
}

.listing-entry-icon {
  display: grid;
  width: 52px;
  height: 52px;
  place-items: center;
  border: 1px solid rgba(124, 92, 231, 0.24);
  border-radius: 15px;
  background: var(--color-bg, #fff);
  color: var(--color-primary, #7c5ce7);
  box-shadow: 0 6px 18px rgba(67, 44, 125, 0.12);
  font-size: 28px;
}

.listing-entry-copy {
  min-width: 0;

  h4 { margin: 2px 0 5px; color: var(--color-text); font-size: 17px; }
  p { max-width: 680px; margin: 0; color: var(--color-text-secondary); font-size: 12px; line-height: 1.65; }
}

.section-kicker {
  color: var(--color-primary, #7c5ce7);
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.14em;
}

.listing-features {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;

  span {
    padding: 3px 8px;
    border: 1px solid rgba(124, 92, 231, 0.18);
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.58);
    color: var(--color-text-secondary);
    font-size: 10px;
  }
}

.listing-entry-action {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 7px;
  min-height: 42px;
  padding-right: 18px;
  padding-left: 16px;
  white-space: nowrap;

  span { font-size: 18px; line-height: 1; }
}

.status-workspace {
  min-width: 0;
  max-width: 100%;
  box-sizing: border-box;
  padding: 18px;
  border: 1px solid var(--color-border);
  border-radius: 14px;
  background: var(--color-surface, #f8f8fb);
}

.workspace-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--color-border);

  h4 { margin: 2px 0 4px; color: var(--color-text); font-size: 16px; }
  p { margin: 0; color: var(--color-text-secondary); font-size: 11px; }
}

.auction-overview {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}

.overview-card {
  --overview-accent: var(--color-primary, #7c5ce7);
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  padding: 12px 14px;
  border: 1px solid var(--color-border);
  border-top: 3px solid var(--overview-accent);
  border-radius: 10px;
  background: var(--color-bg, #fff);
  box-shadow: 0 4px 14px rgba(25, 20, 45, 0.05);

  &--success { --overview-accent: var(--color-success, #2f9e72); }
  &--warning { --overview-accent: var(--color-warning, #d89b26); }
  &--gold { --overview-accent: #b98118; }

  strong {
    display: block;
    margin-top: 2px;
    overflow: hidden;
    color: var(--color-text);
    font-size: 18px;
    line-height: 1.1;
    text-overflow: ellipsis;
  }
}

.overview-icon {
  display: grid;
  flex: 0 0 32px;
  width: 32px;
  height: 32px;
  place-items: center;
  border-radius: 9px;
  background: color-mix(in srgb, var(--overview-accent) 12%, transparent);
  color: var(--overview-accent);
}

.overview-label {
  color: var(--color-text-secondary);
  font-size: 11px;
}

.filter-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
  padding: 8px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: var(--color-bg, #fff);
}

.filter-label {
  align-self: center;
  padding: 0 4px;
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 600;
}

.chip {
  font-size: 12px;
  padding: 3px 12px;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: border-color 0.18s ease, background 0.18s ease, color 0.18s ease;

  &:hover {
    border-color: var(--color-primary);
    color: var(--color-primary);
  }

  &.active {
    border-color: var(--color-primary);
    background: color-mix(in srgb, var(--color-primary) 10%, var(--color-bg));
    color: var(--color-primary);
    font-weight: 600;
  }
}

.refresh-button {
  margin-left: auto;
}

.spinning {
  display: inline-block;
  animation: auction-spin 0.8s linear infinite;
}

@keyframes auction-spin { to { transform: rotate(360deg); } }

.advs1-state.error {
  color: var(--color-danger, #dc3545);
}

.form-hint {
  font-size: 12px;
  color: var(--color-text-secondary);
  margin: 0 0 12px;
}

.lot-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}

.lot-row {
  position: relative;
  min-width: 0;
  max-width: 100%;
  padding: 14px 16px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  box-shadow: 0 3px 12px rgba(25, 20, 45, 0.04);
  transition: border-color 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease;

  &:hover {
    border-color: color-mix(in srgb, var(--color-primary) 38%, var(--color-border));
    box-shadow: 0 7px 20px rgba(25, 20, 45, 0.08);
    transform: translateY(-1px);
  }
}

.lot-main {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  min-width: 0;

  > strong { min-width: 0; overflow-wrap: anywhere; }
}

.status-tag {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-weight: 600;

  &--open { background: rgba(47, 158, 114, 0.12); color: var(--color-success, #27815e); }
  &--settled_won { background: rgba(185, 129, 24, 0.13); color: #9a6812; }
  &--settled_pass { background: rgba(108, 117, 125, 0.12); color: var(--color-text-secondary); }
  &--cancelled { background: rgba(220, 53, 69, 0.1); color: var(--color-danger, #c43d4c); }
}

.meta {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.bid-detail {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
  padding-top: 10px;
  border-top: 1px dashed var(--color-border);
}

.bid-chip {
  font-size: 11px;
  padding: 2px 8px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  color: var(--color-text-secondary);
  min-width: 0;
  overflow-wrap: anywhere;
}

.bid-chip--hidden {
  border-style: dashed;
}

.lot-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}

.fix-summary {
  font-size: 13px;
  margin-bottom: 8px;
}

.fix-note { margin-top: 8px; }

.fix-list {
  max-height: 240px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.fix-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  padding: 4px 8px;
  background: var(--color-surface);
  border-radius: 6px;
  min-width: 0;
}

.fix-lot {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}

.fix-adv {
  color: var(--color-text-secondary);
}

.fix-amount {
  font-weight: 600;
  color: var(--color-warning, #e6a817);
}

.lot-form-row {
  margin: 0 20px 12px;
  padding: 0 14px 14px;
  border: 1px solid var(--color-border);
  border-left: 3px solid var(--color-primary, #7c5ce7);
  border-radius: 12px;
  background: var(--color-bg);
  display: flex;
  flex-direction: column;
  gap: 12px;
  box-shadow: 0 3px 12px rgba(25, 20, 45, 0.04);
  min-width: 0;
  max-width: calc(100% - 40px);
  box-sizing: border-box;
}

.row-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 44px;
  margin: 0 -14px;
  padding: 0 14px;
  border-bottom: 1px solid var(--color-border);
  border-radius: 9px 12px 0 0;
  background: var(--color-surface);
  gap: 10px;
  min-width: 0;
}

.lot-index {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  min-width: 0;

  > span {
    display: grid;
    width: 24px;
    height: 24px;
    place-items: center;
    border-radius: 7px;
    background: var(--color-primary, #7c5ce7);
    color: #fff;
    font-size: 12px;
  }
}

.row-inline {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  align-items: end;
}

.form-block {
  min-width: 0;
  max-width: 100%;
  box-sizing: border-box;
  padding: 12px;
  border: 1px solid var(--color-border);
  border-radius: 9px;
  background: var(--color-surface);
}

.form-block-title {
  display: block;
  margin-bottom: 9px;
  color: var(--color-text-secondary);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.auction-rules-grid {
  display: grid;
  grid-template-columns: minmax(220px, 0.72fr) minmax(0, 1.28fr);
  gap: 12px;
  align-items: stretch;
}

.pricing-panel {
  display: flex;
  min-width: 0;
  flex-direction: column;
  justify-content: space-between;
  gap: 12px;
  padding: 11px 12px;
  border: 1px solid var(--color-border);
  border-radius: 9px;
  background: var(--color-surface);

  strong { display: block; color: var(--color-text); font-size: 12px; }
  > div:first-child > span { color: var(--color-text-secondary); font-size: 10px; }
}

.primary-fields,
.batch-fields,
.uc-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  min-width: 0;
}

.primary-fields > *,
.batch-fields > *,
.uc-fields > *,
.row-inline > *,
.auction-rules-grid > * { min-width: 0; }

.field-group {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 6px;
  margin: 0;
}

.field-label,
.batch-fields .form-label > span {
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 600;

  em { color: var(--color-primary); font-size: 10px; font-style: normal; font-weight: 500; }
  small { margin-left: 5px; font-size: 10px; font-weight: 400; }
}

.uses-left-inline :deep(.form-label) {
  margin-bottom: 0;
}
.uses-left-inline :deep(.field-hint) {
  display: none;
}

.catalog-field {
  position: relative;
}

.catalog-dropdown {
  position: absolute;
  z-index: 20;
  left: 0;
  right: 0;
  max-height: 200px;
  overflow-y: auto;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
}

.catalog-dropdown-title {
  padding: 7px 10px;
  border-bottom: 1px solid var(--color-border);
  color: var(--color-text-secondary);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.08em;
}

.catalog-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 10px;
  border: none;
  background: transparent;
  text-align: left;
  cursor: pointer;
  font-size: 13px;
  min-width: 0;

  &:hover {
    background: var(--color-surface);
  }
}

.catalog-name {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}

.catalog-rarity,
.catalog-type {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.catalog-rule {
  max-width: 220px;
  padding: 2px 6px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--color-primary) 9%, var(--color-bg));
  color: var(--color-primary);
  font-size: 10px;
  overflow-wrap: anywhere;

  &--immutable {
    background: rgba(185, 129, 24, 0.12);
    color: #9a6812;
  }
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
  min-width: 0;
}

.create-modal {
  width: min(1040px, calc(100vw - 32px));
  max-width: 1040px;
  max-height: min(88vh, 900px);
  overflow-y: auto;
  overflow-x: hidden;
  padding: 0;
  box-sizing: border-box;
  overscroll-behavior: contain;
}

.create-heading {
  position: sticky;
  z-index: 5;
  top: 0;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 12px;
  padding: 18px 20px;
  border-bottom: 1px solid var(--color-border);
  background: var(--color-bg, #fff);

  .modal-title { margin: 0 0 3px; }
  .form-hint { margin: 0; }
  > div:nth-child(2) { min-width: 0; }
}

.create-heading-icon {
  display: grid;
  flex: 0 0 40px;
  width: 40px;
  height: 40px;
  place-items: center;
  border: 1px solid color-mix(in srgb, var(--color-primary) 30%, var(--color-border));
  border-radius: 11px;
  background: color-mix(in srgb, var(--color-primary) 10%, var(--color-bg));
  color: var(--color-primary);
  font-size: 22px;
}

.lot-count-badge {
  padding: 5px 10px;
  border-radius: 999px;
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: 11px;
  white-space: nowrap;
}

.modal-close {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  padding: 0;
  border: 1px solid var(--color-border);
  border-radius: 9px;
  background: var(--color-surface, var(--color-bg));
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 21px;
  line-height: 1;
}

.create-section,
.items-section-heading {
  margin-right: 20px;
  margin-left: 20px;
  min-width: 0;
  max-width: calc(100% - 40px);
  box-sizing: border-box;
}

.create-section {
  margin-top: 18px;
  margin-bottom: 20px;
  padding: 14px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: var(--color-bg, #fff);
  background: linear-gradient(135deg, color-mix(in srgb, var(--color-primary, #7c5ce7) 7%, var(--color-bg, #fff)), var(--color-surface));
}

.section-heading,
.items-section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;

  strong { display: block; color: var(--color-text); font-size: 13px; }
  span { display: block; margin-top: 2px; color: var(--color-text-secondary); font-size: 11px; }
}

.section-heading { margin-bottom: 12px; }
.items-section-heading { margin-bottom: 10px; }

.batch-fields .form-label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
}

.input-with-suffix {
  position: relative;
  input { padding-right: 38px; }
  > span { position: absolute; top: 50%; right: 11px; transform: translateY(-50%); }
}

.condition-panel {
  padding: 10px 12px;
  border: 1px dashed var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);

  &.active {
    border-color: color-mix(in srgb, var(--color-primary) 45%, var(--color-border));
    background: color-mix(in srgb, var(--color-primary) 4%, var(--color-surface));
  }
}

.btn-link {
  border: none;
  background: none;
  color: var(--color-primary);
  cursor: pointer;
  font-size: 12px;
}
.remove-lot { color: var(--color-danger, #c43d4c); }

.uc-toggle {
  display: flex;
  align-items: center;
  gap: 9px;
  font-size: 12px;
  color: var(--color-text-secondary);
  cursor: pointer;
  input { position: absolute; opacity: 0; pointer-events: none; }
  > span:last-child { display: flex; flex-direction: column; gap: 1px; }
  strong { color: var(--color-text); font-size: 12px; }
  small { font-size: 10px; }
}

.uc-toggle:has(input:disabled) {
  cursor: default;
  opacity: 0.72;
}

.condition-divider {
  height: 1px;
  margin: 10px 0;
  background: var(--color-border);
}

.toggle-box {
  position: relative;
  width: 32px;
  height: 18px;
  border-radius: 999px;
  background: var(--color-border);
  transition: background 0.18s ease;

  &::after {
    position: absolute;
    top: 3px;
    left: 3px;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: #fff;
    content: '';
    transition: transform 0.18s ease;
  }
}

.uc-toggle input:checked + .toggle-box {
  background: var(--color-primary);
  &::after { transform: translateX(14px); }
}

.uc-fields {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--color-border);
}

.catalog-source-note {
  margin: 10px 0 0;
  padding-top: 10px;
  border-top: 1px solid var(--color-border);
  color: var(--color-text-secondary);
  font-size: 10px;
  line-height: 1.5;
}

.add-lot-button {
  width: calc(100% - 40px);
  margin: 0 20px;
  padding: 11px;
  border: 1px dashed color-mix(in srgb, var(--color-primary) 45%, var(--color-border));
  border-radius: 9px;
  background: color-mix(in srgb, var(--color-primary) 4%, var(--color-bg));
  color: var(--color-primary);
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
  box-sizing: border-box;
  &:hover { background: color-mix(in srgb, var(--color-primary) 9%, var(--color-bg)); }
}

.create-actions {
  position: sticky;
  z-index: 4;
  bottom: 0;
  align-items: center;
  margin-top: 18px;
  padding: 14px 20px;
  border-top: 1px solid var(--color-border);
  background: var(--color-bg, #fff);

  > span { margin-right: auto; color: var(--color-text-secondary); font-size: 11px; }
}

@media (max-width: 900px) {
  .auction-overview { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .row-inline { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .auction-rules-grid { grid-template-columns: 1fr; }
  .create-modal { width: min(760px, calc(100vw - 24px)); }
}

@media (max-width: 600px) {
  .admin-secret-auction { margin-top: 8px; }
  .panel-header { margin-bottom: 12px; }
  .listing-entry { grid-template-columns: auto minmax(0, 1fr); gap: 12px; padding: 16px; }
  .listing-entry-icon { width: 42px; height: 42px; border-radius: 12px; font-size: 23px; }
  .listing-features { display: none; }
  .listing-entry-action { grid-column: 1 / -1; justify-content: center; width: 100%; }
  .status-workspace { padding: 12px; }
  .workspace-heading { align-items: stretch; flex-direction: column; }
  .workspace-heading button { width: 100%; min-height: 40px; }
  .auction-overview { grid-template-columns: 1fr 1fr; gap: 8px; }
  .overview-card { padding: 10px; }
  .overview-icon { display: none; }
  .filter-label { flex-basis: 100%; }
  .chip { flex: 1 1 calc(33.333% - 6px); min-height: 38px; padding: 6px 8px; }
  .refresh-button { width: 100%; min-height: 40px; margin-left: 0; }
  .lot-row { padding: 12px; }
  .lot-main { align-items: flex-start; }
  .lot-main > strong { flex-basis: 100%; font-size: 15px; }
  .meta, .status-tag { line-height: 1.45; }
  .bid-detail { display: grid; grid-template-columns: 1fr; }
  .bid-chip { padding: 6px 8px; }
  .lot-actions { width: 100%; }
  .lot-actions button { width: 100%; min-height: 40px; }
  .modal-mask { place-items: end center; padding: 8px; }
  .modal-box { width: 100%; max-height: calc(100dvh - 8px); border-radius: 16px 16px 8px 8px; }
  .modal-form:not(.create-modal) { padding: 14px; }
  .fix-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: start; padding: 8px; }
  .fix-adv { grid-column: 1 / -1; }
  .create-modal { width: 100%; max-height: calc(100dvh - 8px); border-radius: 16px 16px 8px 8px; }
  .create-heading { grid-template-columns: minmax(0, 1fr) auto; align-items: flex-start; padding: 14px; }
  .create-heading-icon { display: none; }
  .create-heading .form-hint { display: none; }
  .lot-count-badge { grid-column: 1; grid-row: 2; justify-self: start; }
  .modal-close { grid-column: 2; grid-row: 1 / span 2; width: 40px; height: 40px; }
  .create-section, .items-section-heading, .lot-form-row { margin-right: 12px; margin-left: 12px; }
  .create-section, .items-section-heading, .lot-form-row { max-width: calc(100% - 24px); }
  .add-lot-button { width: calc(100% - 24px); margin-right: 12px; margin-left: 12px; }
  .batch-fields, .primary-fields, .row-inline, .uc-fields, .auction-rules-grid { grid-template-columns: 1fr; }
  .items-section-heading { align-items: stretch; flex-direction: column; }
  .items-section-heading > div span { display: none; }
  .items-section-heading > button { width: 100%; min-height: 40px; }
  .row-head { min-height: 48px; }
  .form-block, .pricing-panel, .condition-panel { padding: 10px; }
  .catalog-item { align-items: flex-start; flex-wrap: wrap; }
  .catalog-name { flex-basis: 100%; }
  .create-actions { flex-wrap: wrap; padding: 12px; }
  .create-actions > span { width: 100%; margin-bottom: 2px; }
  .create-actions button { flex: 1 1 100%; min-height: 42px; }
}
</style>
