<template>
  <div class="item-holder-panel">
    <div class="search-bar">
      <div class="search-field">
        <input
          v-model="search"
          type="text"
          class="search-input"
          placeholder="输入物品名称（至少 2 字）…"
          @keydown.enter="doSearch(1)"
        >
        <div v-if="catalogResults.length > 0" class="catalog-dropdown">
          <button
            v-for="c in catalogResults"
            :key="c.id"
            type="button"
            class="catalog-item"
            @click="pickCatalog(c.name)"
          >
            <span class="catalog-name">{{ c.name }}</span>
            <span class="catalog-meta">{{ c.rarity }} · {{ c.type || '—' }}</span>
          </button>
        </div>
      </div>
      <label class="exact-toggle">
        <input v-model="exactMatch" type="checkbox">
        精确匹配
      </label>
      <button type="button" class="btn-primary btn-sm" :disabled="loading" @click="doSearch(1)">
        搜索
      </button>
    </div>

    <p v-if="error" class="error-text">{{ error }}</p>
    <div v-else-if="loading" class="empty">搜索中…</div>
    <div v-else-if="searched && items.length === 0" class="empty">未找到持有「{{ lastSearch }}」的记录</div>
    <template v-else-if="searched">
      <div v-if="summary" class="summary-bar">
        <span>共 {{ summary.total_records }} 条记录</span>
        <span>{{ summary.unique_adventurers }} 名冒险者</span>
        <span>{{ summary.unique_owners }} 名玩家</span>
        <span v-if="summary.truncated" class="warn">结果过多，仅展示前 1000 条，请缩小搜索范围</span>
      </div>
      <div class="table-wrap">
        <table class="holder-table">
          <thead>
            <tr>
              <th>物品</th>
              <th>位置</th>
              <th>数量</th>
              <th>冒险者</th>
              <th>玩家</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in items" :key="`${row.location}-${row.record_id}`">
              <td data-label="物品">
                <span class="item-name">{{ row.item_name }}</span>
                <small v-if="row.has_custom_desc" class="muted">个性化</small>
              </td>
              <td data-label="位置">{{ locationLabel(row.location) }}</td>
              <td data-label="数量">
                {{ row.quantity }}
                <span v-if="row.uses_left != null" class="muted">/ {{ row.uses_left }} 次</span>
              </td>
              <td data-label="冒险者">#{{ row.adventurer_id }} {{ row.adventurer_name }}</td>
              <td data-label="玩家">
                <template v-if="row.owner_id">
                  {{ row.nickname || row.username }}
                  <small class="muted">UID {{ row.owner_id }}</small>
                </template>
                <span v-else class="muted">系统/NPC</span>
              </td>
              <td data-label="状态">{{ statusLabel(row.adventurer_status) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <Pagination v-if="pagination" :pagination="pagination" @go="doSearch" />
    </template>
    <p v-else class="empty hint">搜索物品名称，查看哪些冒险者（及对应玩家）持有该物品。</p>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import Pagination from '@/components/Pagination.vue'
import { useAdventurerS1Store } from '../stores/adventurerS1'

const store = useAdventurerS1Store()

const search = ref('')
const exactMatch = ref(false)
const loading = ref(false)
const searched = ref(false)
const error = ref('')
const lastSearch = ref('')
const items = ref<any[]>([])
const summary = ref<any>(null)
const pagination = ref<any>(null)
const catalogResults = ref<any[]>([])

let catalogTimer: ReturnType<typeof setTimeout> | null = null

function locationLabel(loc: string) {
  return loc === 'warehouse' ? '仓库' : '随身'
}

function statusLabel(s: string) {
  const m: Record<string, string> = {
    resting: '休息', in_party: '组队', exploring: '探险', building: '建造',
    skill_school_queue: '等候导师指点',
    building_queue: '等候设施空位',
    dead: '死亡', archived: '封存',
  }
  return m[s] || s
}

function pickCatalog(name: string) {
  search.value = name
  catalogResults.value = []
  exactMatch.value = true
  doSearch(1)
}

watch(search, (val) => {
  if (catalogTimer) clearTimeout(catalogTimer)
  const q = val.trim()
  if (q.length < 2) {
    catalogResults.value = []
    return
  }
  catalogTimer = setTimeout(async () => {
    const res = await store.adminSearchItemCatalog({ search: q })
    if (res?.code === 200) {
      catalogResults.value = res.data || []
    }
  }, 300)
})

async function doSearch(page = 1) {
  const q = search.value.trim()
  if (q.length < 2) {
    error.value = '搜索词至少 2 个字符'
    return
  }
  loading.value = true
  error.value = ''
  catalogResults.value = []
  try {
    const res = await store.adminSearchItemHolders({
      search: q,
      match: exactMatch.value ? 'exact' : 'contains',
      page,
      per_page: 30,
    })
    if (res?.code !== 200) {
      error.value = res?.message || '搜索失败'
      items.value = []
      summary.value = null
      pagination.value = null
      return
    }
    searched.value = true
    lastSearch.value = q
    items.value = res.data?.items || []
    summary.value = res.data?.summary || null
    pagination.value = {
      current_page: res.data?.page || 1,
      pages: res.data?.pages || 1,
      total: res.data?.total || 0,
    }
  } finally {
    loading.value = false
  }
}
</script>

<style scoped lang="scss">
.item-holder-panel {
  margin-top: 8px;
  min-width: 0;
  max-width: 100%;
}

.search-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  align-items: center;
  margin-bottom: 12px;
}

.search-field {
  position: relative;
  flex: 1;
  min-width: 220px;
  max-width: 100%;
}

.search-input {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
}

.exact-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  white-space: nowrap;
}

.catalog-dropdown {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  z-index: 20;
  max-height: 220px;
  overflow-y: auto;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
}

.catalog-item {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 8px 10px;
  border: none;
  background: transparent;
  color: var(--color-text);
  text-align: left;
  cursor: pointer;

  &:hover {
    background: var(--color-bg);
  }
}

.catalog-meta {
  color: var(--color-text-secondary);
  font-size: 12px;
}

.summary-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 10px;
  font-size: 13px;
  color: var(--color-text-secondary);

  .warn {
    color: #f0a020;
  }
}

.table-wrap {
  max-width: 100%;
  overflow-x: auto;
}

.holder-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;

  th, td {
    border: 1px solid var(--color-border);
    padding: 6px 8px;
    text-align: left;
  }
}

.item-name {
  font-weight: 600;
}

.muted {
  color: var(--color-text-secondary);
}

.empty {
  padding: 20px;
  text-align: center;
  color: var(--color-text-secondary);
}

.hint {
  font-size: 13px;
}

.error-text {
  color: #e88080;
  margin-bottom: 8px;
}

@media (max-width: 720px) {
  .search-bar { display: grid; grid-template-columns: 1fr; align-items: stretch; }
  .search-field { min-width: 0; }
  .search-bar > button { width: 100%; min-height: 40px; }
  .exact-toggle { min-height: 36px; }
  .summary-bar { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
  .summary-bar span { min-width: 0; overflow-wrap: anywhere; }
  .summary-bar .warn { grid-column: 1 / -1; }
  .holder-table, .holder-table tbody, .holder-table tr, .holder-table td { display: block; width: 100%; box-sizing: border-box; }
  .holder-table thead { display: none; }
  .holder-table tr { margin-bottom: 10px; overflow: hidden; border: 1px solid var(--color-border); border-radius: 8px; background: var(--color-bg); }
  .holder-table td { display: grid; grid-template-columns: minmax(70px, auto) minmax(0, 1fr); gap: 10px; border: 0; border-bottom: 1px solid var(--color-border); overflow-wrap: anywhere; }
  .holder-table td:last-child { border-bottom: 0; }
  .holder-table td::before { content: attr(data-label); color: var(--color-text-secondary); font-weight: 600; }
}

@media (max-width: 420px) {
  .summary-bar { grid-template-columns: 1fr; }
  .summary-bar .warn { grid-column: auto; }
}
</style>
