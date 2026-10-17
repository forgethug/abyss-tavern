<template>
  <section class="advs1-card">
    <div class="panel-header">
      <h3 class="col-title">🎡 幸运大转盘奖池管理</h3>
      <div class="panel-actions">
        <button type="button" class="btn-secondary" @click="openAddCatalog">+ 图鉴物品</button>
        <button type="button" class="advs1-submit" @click="openAddCustom">+ 自定义奖品</button>
      </div>
    </div>

    <div v-if="loading" class="advs1-state">加载中…</div>
    <div v-else-if="!poolData" class="advs1-state">加载失败</div>
    <template v-else>
      <div v-for="tier in poolData.tiers" :key="tier.rarity" class="pool-tier">
        <div class="tier-head">
          <span class="tier-rarity" :style="{ color: rarityColor(tier.rarity) }">
            {{ rarityLabel(tier.rarity) }}
          </span>
          <span class="tier-prob">概率 {{ tier.probability_percent }}%</span>
          <span class="tier-count">{{ tier.items.length }} 件</span>
        </div>
        <div v-if="tier.items.length === 0" class="tier-empty">暂无奖品</div>
        <div v-else class="tier-items">
          <div v-for="item in tier.items" :key="item.name" :class="['tier-item', { excluded: item.excluded }]">
            <div class="tier-item-head">
              <span class="tier-item-name" :style="{ color: rarityColor(item.rarity) }">
                {{ item.name }}
              </span>
              <span v-if="item.world_unique" class="badge badge-unique">全服唯一</span>
              <span v-if="item.is_fallback" class="badge badge-fallback">保底</span>
              <span v-if="item.text_locked" class="badge badge-locked">不可变</span>
              <span v-if="item.current" class="badge badge-current">当前</span>
              <span v-if="item.excluded" class="badge badge-excluded">已排除</span>
              <span :class="['badge', sourceBadgeClass(item.source)]">
                {{ sourceBadgeLabel(item.source) }}
              </span>
              <span v-if="poolItemUsesLeft(item) != null" class="badge badge-uses">
                🔢 {{ poolItemUsesLeft(item) }} 次
              </span>
            </div>
            <p v-if="item.description" class="tier-item-desc">{{ item.description }}</p>
            <div v-if="!item.is_fallback" class="tier-item-actions">
              <button
                v-if="!item.excluded"
                type="button"
                class="btn-secondary btn-sm"
                @click="openEdit(item)"
              >编辑</button>
              <button
                v-if="!item.excluded"
                type="button"
                class="btn-reject btn-sm"
                :disabled="removingName === item.name"
                @click="confirmRemove(item)"
              >
                {{ removingName === item.name ? '移除中…' : '移除' }}
              </button>
              <button
                v-if="item.excluded"
                type="button"
                class="btn-success btn-sm"
                :disabled="removingName === item.name"
                @click="confirmRestore(item)"
              >
                {{ removingName === item.name ? '恢复中…' : '恢复' }}
              </button>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- 新增自定义奖品弹窗 -->
    <Teleport to="body">
      <div v-if="showAddCustom" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="showAddCustom = false">
        <div class="modal-container s1-mobile-dialog">
          <div class="modal-header s1-mobile-dialog-header">
            <h4>🎡 新增自定义奖品</h4>
            <button type="button" class="btn-close" @click="showAddCustom = false">&times;</button>
          </div>
          <div class="modal-body s1-mobile-dialog-body">
            <label class="form-label">
              物品名称
              <input v-model="form.name" type="text" class="advs1-input" placeholder="输入物品名称" maxlength="100" />
            </label>
            <label class="form-label">
              描述
              <textarea v-model="form.description" class="advs1-input" rows="3" placeholder="物品描述"></textarea>
            </label>
            <label class="form-label">
              稀有度
              <select v-model="form.rarity" class="advs1-input">
                <option value="common">普通</option>
                <option value="rare">稀有</option>
                <option value="epic">史诗</option>
                <option value="legendary">传说</option>
              </select>
            </label>
            <label v-if="form.rarity === 'legendary'" class="form-label">
              轮换位置
              <select v-model="form.legendary_position" class="advs1-input">
                <option value="current">当期（当前可抽取）</option>
                <option value="next">下期（当前被抽走后上架）</option>
              </select>
            </label>
            <label class="form-label">
              类型
              <select v-model="form.item_type" class="advs1-input">
                <option v-for="t in ITEM_TYPE_OPTIONS" :key="t.value" :value="t.value">{{ t.label }}</option>
              </select>
            </label>
            <label class="form-label checkbox-label">
              <input v-model="form.world_unique" type="checkbox" :disabled="form.rarity === 'legendary'" />
              全服唯一（抽走后不再出现）
              <span v-if="form.rarity === 'legendary'" class="form-hint">（传说物品自动全服唯一）</span>
            </label>
            <label class="form-label checkbox-label">
              <input v-model="form.text_locked" type="checkbox" />
              不可变（不可锻造/炼金/嬗变，申诉不可改）
            </label>
            <AdminItemUsesLeftField v-model="form.uses_left" />
          </div>
          <div class="modal-footer s1-mobile-dialog-footer">
            <button type="button" class="btn-secondary" @click="showAddCustom = false">取消</button>
            <button
              type="button"
              class="advs1-submit"
              :disabled="!form.name.trim() || submitting"
              @click="doAddCustom"
            >
              {{ submitting ? '添加中…' : '添加' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 新增图鉴物品弹窗 -->
    <Teleport to="body">
      <div v-if="showAddCatalog" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="showAddCatalog = false">
        <div class="modal-container s1-mobile-dialog">
          <div class="modal-header s1-mobile-dialog-header">
            <h4>📚 从图鉴加入奖池</h4>
            <button type="button" class="btn-close" @click="showAddCatalog = false">&times;</button>
          </div>
          <div class="modal-body s1-mobile-dialog-body">
            <label class="form-label">
              搜索图鉴
              <div class="catalog-field">
                <input
                  v-model="catalogForm.search"
                  type="text"
                  class="advs1-input"
                  placeholder="输入物品名称搜索"
                  @input="onCatalogSearchInput"
                  @focus="catalogDropdownOpen = true"
                />
                <div v-if="catalogDropdownOpen && catalogResults.length > 0" class="catalog-dropdown">
                  <button
                    v-for="c in catalogResults"
                    :key="c.id"
                    type="button"
                    class="catalog-item"
                    @click="pickCatalogItem(c)"
                  >
                    <span class="catalog-name">{{ c.name }}</span>
                    <span class="catalog-rarity">{{ rarityLabel(c.rarity) }}</span>
                    <span class="catalog-type">{{ c.type }}</span>
                  </button>
                </div>
              </div>
            </label>
            <p v-if="catalogForm.pickedName" class="form-hint">
              已选：{{ catalogForm.pickedName }}
              <span v-if="catalogForm.pickedRarity">（图鉴稀有度 {{ rarityLabel(catalogForm.pickedRarity) }}）</span>
            </p>
            <label class="form-label">
              加入奖池稀有度
              <select v-model="catalogForm.rarity" class="advs1-input">
                <option value="common">普通</option>
                <option value="rare">稀有</option>
                <option value="epic">史诗</option>
                <option value="legendary">传说</option>
              </select>
            </label>
            <label v-if="catalogForm.rarity === 'legendary'" class="form-label">
              轮换位置
              <select v-model="catalogForm.legendary_position" class="advs1-input">
                <option value="current">当期（当前可抽取）</option>
                <option value="next">下期（当前被抽走后上架）</option>
              </select>
            </label>
            <label class="form-label checkbox-label">
              <input
                v-model="catalogForm.text_locked"
                type="checkbox"
                :disabled="catalogForm.pickedLocked"
              />
              不可变（不可锻造/炼金/嬗变，申诉不可改）
              <span v-if="catalogForm.pickedLocked" class="form-hint">（图鉴已为不可变）</span>
            </label>
            <AdminItemUsesLeftField v-model="catalogForm.uses_left" />
          </div>
          <div class="modal-footer s1-mobile-dialog-footer">
            <button type="button" class="btn-secondary" @click="showAddCatalog = false">取消</button>
            <button
              type="button"
              class="advs1-submit"
              :disabled="!catalogForm.pickedName || catalogSubmitting"
              @click="doAddCatalog"
            >
              {{ catalogSubmitting ? '添加中…' : '添加' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 编辑奖品弹窗 -->
    <Teleport to="body">
      <div v-if="showEdit" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="showEdit = false">
        <div class="modal-container s1-mobile-dialog">
          <div class="modal-header s1-mobile-dialog-header">
            <h4>✏️ 编辑奖品</h4>
            <button type="button" class="btn-close" @click="showEdit = false">&times;</button>
          </div>
          <div class="modal-body s1-mobile-dialog-body">
            <label class="form-label">
              物品名称
              <input v-model="editForm.name" type="text" class="advs1-input" placeholder="输入物品名称" maxlength="100" />
            </label>
            <label class="form-label">
              描述
              <textarea v-model="editForm.description" class="advs1-input" rows="3" placeholder="物品描述"></textarea>
            </label>
            <label class="form-label">
              类型
              <select v-model="editForm.item_type" class="advs1-input">
                <option v-for="t in ITEM_TYPE_OPTIONS" :key="t.value" :value="t.value">{{ t.label }}</option>
              </select>
            </label>
            <label class="form-label checkbox-label">
              <input v-model="editForm.world_unique" type="checkbox" />
              全服唯一（抽走后不再出现）
            </label>
            <label class="form-label checkbox-label">
              <input
                v-model="editForm.text_locked"
                type="checkbox"
                :disabled="editFormLockedImmutable"
              />
              不可变（不可锻造/炼金/嬗变，申诉不可改）
              <span v-if="editFormLockedImmutable" class="form-hint">（系统锁定，不可取消）</span>
            </label>
            <AdminItemUsesLeftField v-model="editForm.uses_left" />
            <p class="form-hint">稀有度：{{ rarityLabel(editingItem?.rarity ?? '') }}（不可修改，如需改稀有度请移除后重新添加）</p>
          </div>
          <div class="modal-footer s1-mobile-dialog-footer">
            <button type="button" class="btn-secondary" @click="showEdit = false">取消</button>
            <button
              type="button"
              class="advs1-submit"
              :disabled="!editForm.name.trim() || editSubmitting"
              @click="doEdit"
            >
              {{ editSubmitting ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { useAdventurerS1Store } from '../stores/adventurerS1'
import { rarityColor, rarityLabel, ITEM_TYPE_OPTIONS, S1_DEFAULT_ITEM_TYPE, isKnownItemType } from '../utils'
import { appendUsesLeftPayload, extractDefaultUsesLeft } from '../composables/adminItemForm'
import AdminItemUsesLeftField from './AdminItemUsesLeftField.vue'

const store = useAdventurerS1Store()
const message = useMessage()

const loading = ref(false)
const submitting = ref(false)
const catalogSubmitting = ref(false)
const removingName = ref<string | null>(null)
const showAddCustom = ref(false)
const showAddCatalog = ref(false)
const showEdit = ref(false)
const editSubmitting = ref(false)
const editingItem = ref<PoolItem | null>(null)
const catalogResults = ref<CatalogSearchItem[]>([])
const catalogDropdownOpen = ref(false)
let catalogTimer: ReturnType<typeof setTimeout> | null = null

interface PoolItem {
  name: string
  type: string
  description: string
  rarity: string
  world_unique?: boolean
  text_locked?: boolean
  system_locked?: boolean
  default_uses_left?: number | null
  is_fallback?: boolean
  source: 'base' | 'custom' | 'fallback'
  current?: boolean
  excluded?: boolean
}

interface CatalogSearchItem {
  id: number
  name: string
  type: string
  rarity: string
  text_locked?: boolean
  is_hidden_boss_clue?: boolean
  default_uses_left?: number | null
  use_condition?: Record<string, unknown> | null
}

interface PoolTier {
  rarity: string
  probability_percent: number
  items: PoolItem[]
}

const poolData = ref<{ tiers: PoolTier[]; custom_pool_exists: boolean } | null>(null)

const form = reactive({
  name: '',
  description: '',
  rarity: 'common',
  item_type: S1_DEFAULT_ITEM_TYPE,
  world_unique: false,
  text_locked: false,
  uses_left: null as number | null,
  legendary_position: 'next' as 'current' | 'next',
})

const catalogForm = reactive({
  search: '',
  pickedName: '',
  pickedRarity: '',
  pickedLocked: false,
  rarity: 'common',
  text_locked: false,
  uses_left: null as number | null,
  legendary_position: 'next' as 'current' | 'next',
})

const editForm = reactive({
  name: '',
  description: '',
  item_type: S1_DEFAULT_ITEM_TYPE,
  world_unique: false,
  text_locked: false,
  uses_left: null as number | null,
})

const editFormLockedImmutable = computed(
  () => !!editingItem.value?.system_locked,
)

function poolItemUsesLeft(item: PoolItem): number | null {
  return item.default_uses_left ?? null
}

function sourceBadgeClass(source: PoolItem['source']) {
  if (source === 'custom') return 'badge-custom'
  if (source === 'fallback') return 'badge-fallback'
  return 'badge-base'
}

function sourceBadgeLabel(source: PoolItem['source']) {
  if (source === 'custom') return '自定义'
  if (source === 'fallback') return '保底'
  return '基础'
}

async function loadPool() {
  loading.value = true
  try {
    const res = await store.fetchAdminLuckyWheelPool({ _t: Date.now() })
    if (res.code === 200 && res.data) {
      poolData.value = res.data
    } else {
      message.error(res.message || '加载奖池失败')
    }
  } finally {
    loading.value = false
  }
}

function openAddCustom() {
  form.name = ''
  form.description = ''
  form.rarity = 'common'
  form.item_type = S1_DEFAULT_ITEM_TYPE
  form.world_unique = false
  form.text_locked = false
  form.uses_left = null
  form.legendary_position = 'next'
  showAddCustom.value = true
}

function openAddCatalog() {
  catalogForm.search = ''
  catalogForm.pickedName = ''
  catalogForm.pickedRarity = ''
  catalogForm.pickedLocked = false
  catalogForm.rarity = 'common'
  catalogForm.text_locked = false
  catalogForm.uses_left = null
  catalogForm.legendary_position = 'next'
  catalogResults.value = []
  catalogDropdownOpen.value = false
  showAddCatalog.value = true
}

function onCatalogSearchInput() {
  if (catalogTimer) clearTimeout(catalogTimer)
  const q = catalogForm.search.trim()
  if (!q) {
    catalogResults.value = []
    return
  }
  catalogTimer = setTimeout(async () => {
    try {
      const res = await store.adminSearchItemCatalog({ search: q })
      catalogResults.value = res.code === 200 && Array.isArray(res.data) ? res.data : []
    } catch {
      catalogResults.value = []
    }
  }, 250)
}

function pickCatalogItem(item: CatalogSearchItem) {
  catalogForm.pickedName = item.name
  catalogForm.pickedRarity = item.rarity
  catalogForm.pickedLocked = !!(item.text_locked || item.is_hidden_boss_clue)
  catalogForm.search = item.name
  catalogForm.rarity = item.rarity || 'common'
  catalogForm.text_locked = catalogForm.pickedLocked
  catalogForm.uses_left = extractDefaultUsesLeft(item.use_condition, item.default_uses_left ?? null)
  catalogDropdownOpen.value = false
  catalogResults.value = []
}

async function doAddCustom() {
  if (!form.name.trim()) return
  submitting.value = true
  try {
    const isLegendary = form.rarity === 'legendary'
    const payload: Record<string, unknown> = {
      name: form.name.trim(),
      description: form.description.trim(),
      rarity: form.rarity,
      item_type: form.item_type,
      world_unique: isLegendary ? true : form.world_unique,
      text_locked: form.text_locked,
      legendary_position: isLegendary ? form.legendary_position : undefined,
    }
    appendUsesLeftPayload(payload, form.uses_left)
    const res = await store.addAdminLuckyWheelItem(payload as Parameters<typeof store.addAdminLuckyWheelItem>[0])
    if (res.code === 200 || res.code === 201) {
      message.success(res.message || '添加成功')
      showAddCustom.value = false
      await loadPool()
    } else {
      message.error(res.message || '添加失败')
    }
  } finally {
    submitting.value = false
  }
}

async function doAddCatalog() {
  if (!catalogForm.pickedName) return
  catalogSubmitting.value = true
  try {
    const isLegendary = catalogForm.rarity === 'legendary'
    const payload: Record<string, unknown> = {
      from_catalog: true,
      name: catalogForm.pickedName,
      rarity: catalogForm.rarity,
      text_locked: catalogForm.text_locked,
      legendary_position: isLegendary ? catalogForm.legendary_position : undefined,
    }
    appendUsesLeftPayload(payload, catalogForm.uses_left)
    const res = await store.addAdminLuckyWheelItem(payload as Parameters<typeof store.addAdminLuckyWheelItem>[0])
    if (res.code === 200 || res.code === 201) {
      message.success(res.message || '添加成功')
      showAddCatalog.value = false
      await loadPool()
    } else {
      message.error(res.message || '添加失败')
    }
  } finally {
    catalogSubmitting.value = false
  }
}

function openEdit(item: PoolItem) {
  editingItem.value = item
  editForm.name = item.name
  editForm.description = item.description
  editForm.item_type = isKnownItemType(item.type) ? item.type : S1_DEFAULT_ITEM_TYPE
  editForm.world_unique = item.world_unique ?? false
  editForm.text_locked = item.text_locked ?? false
  editForm.uses_left = item.default_uses_left ?? null
  showEdit.value = true
}

async function doEdit() {
  if (!editForm.name.trim() || !editingItem.value) return
  editSubmitting.value = true
  try {
    const payload: Record<string, unknown> = {
      rarity: editingItem.value.rarity,
      new_name: editForm.name.trim() !== editingItem.value.name ? editForm.name.trim() : undefined,
      description: editForm.description.trim(),
      item_type: editForm.item_type,
      world_unique: editForm.world_unique,
      text_locked: editForm.text_locked,
    }
    appendUsesLeftPayload(payload, editForm.uses_left)
    const res = await store.editAdminLuckyWheelItem(editingItem.value.name, payload as Parameters<typeof store.editAdminLuckyWheelItem>[1])
    if (res.code === 200) {
      message.success(res.message || '已更新')
      showEdit.value = false
      await loadPool()
    } else {
      message.error(res.message || '更新失败')
    }
  } finally {
    editSubmitting.value = false
  }
}

async function confirmRemove(item: PoolItem) {
  const isBase = item.source === 'base'
  const hint = isBase ? '（基础物品将被排除，可随时恢复）' : '（图鉴条目不会删除）'
  if (!confirm(`确定要从奖池移除「${item.name}」吗？${hint}`)) return
  removingName.value = item.name
  try {
    const res = await store.removeAdminLuckyWheelItem(item.name, item.rarity)
    if (res.code === 200) {
      message.success(res.message || '已移除')
      await loadPool()
    } else {
      message.error(res.message || '移除失败')
    }
  } finally {
    removingName.value = null
  }
}

async function confirmRestore(item: PoolItem) {
  removingName.value = item.name
  try {
    const res = await store.restoreAdminLuckyWheelItem(item.name, item.rarity)
    if (res.code === 200) {
      message.success(res.message || '已恢复')
      await loadPool()
    } else {
      message.error(res.message || '恢复失败')
    }
  } finally {
    removingName.value = null
  }
}

onMounted(() => {
  void loadPool()
})
</script>

<style scoped>
.panel-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  margin-bottom: 1rem;
}
.panel-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}
.catalog-field {
  position: relative;
}
.catalog-dropdown {
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  z-index: 10;
  max-height: 220px;
  overflow-y: auto;
  margin-top: 0.25rem;
  border: 1px solid var(--color-border, #ddd);
  border-radius: 8px;
  background: var(--color-bg, #fff);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
}
.catalog-item {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem;
  width: 100%;
  padding: 0.45rem 0.6rem;
  border: none;
  border-bottom: 1px solid var(--color-border, #eee);
  background: transparent;
  cursor: pointer;
  text-align: left;
}
.catalog-item:last-child {
  border-bottom: none;
}
.catalog-item:hover {
  background: var(--color-surface, #f5f5f5);
}
.catalog-name {
  font-weight: 500;
}
.catalog-rarity,
.catalog-type {
  font-size: 0.75rem;
  color: var(--color-text-secondary, #666);
}
.badge-locked {
  background: #e0e7ff;
  color: #3730a3;
  border: 1px solid #a5b4fc;
}
.pool-tier {
  margin-bottom: 1rem;
  padding-bottom: 0.75rem;
  border-bottom: 1px solid var(--color-border, #eee);
}
.pool-tier:last-child {
  border-bottom: none;
}
.tier-head {
  display: flex;
  align-items: baseline;
  gap: 0.75rem;
  margin-bottom: 0.5rem;
}
.tier-rarity {
  font-weight: 600;
}
.tier-prob {
  font-size: 0.85rem;
  color: var(--color-text-secondary, #666);
}
.tier-count {
  font-size: 0.85rem;
  color: var(--color-text-secondary, #666);
}
.tier-empty {
  font-size: 0.85rem;
  color: var(--color-text-secondary, #999);
  padding: 0.5rem 0;
}
.tier-items {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.tier-item {
  padding: 0.5rem 0.65rem;
  border-radius: 6px;
  background: var(--color-bg, #fff);
  border: 1px solid var(--color-border, #eee);
}
.tier-item.excluded {
  opacity: 0.55;
  border-style: dashed;
}
.tier-item-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem;
  margin-bottom: 0.2rem;
}
.tier-item-name {
  font-weight: 500;
}
.tier-item-desc {
  font-size: 0.8rem;
  color: var(--color-text-secondary, #666);
  line-height: 1.4;
  margin: 0.2rem 0 0.4rem;
}
.tier-item-actions {
  display: flex;
  gap: 0.5rem;
}
.badge {
  font-size: 0.72rem;
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
}
.badge-unique {
  background: #fef3c7;
  color: #92400e;
  border: 1px solid #fcd34d;
}
.badge-current {
  background: #dcfce7;
  color: #166534;
  border: 1px solid #86efac;
}
.badge-custom {
  background: #ede9fe;
  color: #5b21b6;
  border: 1px solid #c4b5fd;
}
.badge-base {
  background: var(--color-surface, #f5f5f5);
  color: var(--color-text-secondary, #666);
  border: 1px solid var(--color-border, #ddd);
}
.badge-fallback {
  background: #ecfdf5;
  color: #047857;
  border: 1px solid #6ee7b7;
}
.badge-excluded {
  background: #fee2e2;
  color: #991b1b;
  border: 1px solid #fca5a5;
}
.badge-uses {
  background: rgba(99, 102, 241, 0.12);
  color: #4338ca;
  border: 1px solid rgba(99, 102, 241, 0.35);
}
.btn-success {
  background: #dcfce7;
  color: #166534;
  border: 1px solid #86efac;
  cursor: pointer;
}
.btn-success:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-sm {
  font-size: 0.8rem;
  padding: 0.2rem 0.5rem;
}
.btn-secondary {
  background: var(--color-surface, #f5f5f5);
  color: var(--color-text, #333);
  border: 1px solid var(--color-border, #ddd);
  cursor: pointer;
}
.btn-secondary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.form-hint {
  font-size: 0.8rem;
  color: var(--color-text-secondary, #999);
  margin-top: 0.25rem;
}

/* ── Teleport 弹窗样式（必须在组件内完整定义） ── */
.modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1100;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.5);
}
.modal-container {
  background: var(--color-bg, #fff);
  border-radius: 12px;
  width: min(480px, 90vw);
  max-height: 85vh;
  overflow-y: auto;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.2);
}
.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 1rem 1.25rem;
  border-bottom: 1px solid var(--color-border, #eee);
}
.modal-header h4 {
  margin: 0;
  font-size: 1.1rem;
}
.btn-close {
  background: none;
  border: none;
  font-size: 1.5rem;
  cursor: pointer;
  color: var(--color-text-secondary, #666);
  padding: 0;
  line-height: 1;
}
.modal-body {
  padding: 1rem 1.25rem;
}
.form-label {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  margin-bottom: 0.75rem;
  font-size: 0.9rem;
  font-weight: 500;
}
.checkbox-label {
  flex-direction: row;
  align-items: center;
  gap: 0.5rem;
}
.form-hint {
  font-size: 0.8rem;
  color: var(--color-text-secondary, #999);
  font-weight: 400;
}
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  padding: 0.75rem 1.25rem;
  border-top: 1px solid var(--color-border, #eee);
}
</style>
