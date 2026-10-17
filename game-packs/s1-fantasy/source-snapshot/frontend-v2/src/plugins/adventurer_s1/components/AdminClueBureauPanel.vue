<template>
  <div class="admin-clue-bureau">
    <p class="admin-hint">
      独立于任务栏「隐藏 Boss 讨伐」进度。追查池决定线索追查处可随机掉落的 Boss 范围（上限 {{ preview?.pool_cap ?? 10 }} 条）。
    </p>

    <div class="pool-summary">
      <div class="summary-stat">
        <span class="stat-label">追查池</span>
        <strong>{{ preview?.pool_count ?? 0 }} / {{ preview?.pool_cap ?? 10 }}</strong>
      </div>
      <div class="summary-stat">
        <span class="stat-label">可掉落</span>
        <strong>{{ preview?.grant_eligible_count ?? 0 }}</strong>
      </div>
      <div class="summary-stat">
        <span class="stat-label">图鉴总数</span>
        <strong>{{ preview?.catalog_boss_count ?? 0 }}</strong>
      </div>
    </div>

    <p v-if="(preview?.need_count ?? preview?.would_add_count ?? 0) > 0" class="admin-hint">
      点击补充后将调用 AI 生成 {{ preview?.need_count ?? preview?.would_add_count }} 名追查 Boss（地牢名取自 S1 副本列表）。
    </p>
    <p v-else-if="supplementBlockHint" class="admin-hint admin-hint-warn">{{ supplementBlockHint }}</p>

    <div class="admin-actions">
      <button
        type="button"
        class="btn-secondary btn-sm"
        :disabled="loading"
        @click="loadPreview"
      >🔄 刷新</button>
      <button
        type="button"
        class="btn-primary btn-sm"
        :disabled="supplementing || !preview?.can_supplement"
        @click="onSupplement"
      >➕ 补充追查池至上限</button>
    </div>

    <div v-if="loading && !preview" class="empty-hint">加载中…</div>

    <template v-else>
      <h6 class="block-title">追查池内 Boss（{{ poolBosses.length }}）</h6>
      <p v-if="poolBosses.length === 0" class="empty-hint">追查池为空，请补充 Boss。</p>
      <div v-else class="boss-list">
        <details v-for="boss in poolBosses" :key="boss.boss_id" class="boss-card" open>
          <summary class="boss-summary">
            <span class="boss-name">{{ boss.boss_name }}</span>
            <span class="boss-dungeon">🗺️ {{ boss.dungeon }}<template v-if="boss.dungeon_level_desc">（{{ boss.dungeon_level_desc }}）</template></span>
            <span :class="['status-pill', 'st-' + boss.status]">{{ statusLabel(boss.status) }}</span>
            <span v-if="boss.clue_eligible" class="eligible-pill">可掉落线索</span>
            <button
              v-if="boss.status === 'active'"
              type="button"
              class="btn-warning btn-sm release-lock-btn"
              :disabled="releasingBossId === boss.boss_id"
              @click.stop="onReleaseLock(boss)"
            >🔓 释放锁定</button>
          </summary>
          <div class="boss-body">
            <p class="boss-id muted small">ID：{{ boss.boss_id }}</p>
            <p v-if="boss.status === 'active'" class="lock-info muted small">
              锁定小队 #{{ boss.active_party_id ?? '—' }}
              <template v-if="boss.active_expedition_log_id"> · 探险日志 #{{ boss.active_expedition_log_id }}</template>
            </p>
            <div class="background-block">
              <strong>背景</strong>
              <p>{{ boss.background }}</p>
            </div>
            <table class="clue-table">
              <thead>
                <tr>
                  <th>序号</th>
                  <th>物品名</th>
                  <th>描述</th>
                  <th>线索文本</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="c in boss.clues" :key="c.index">
                  <td data-label="序号">{{ c.index }}/4</td>
                  <td data-label="物品名">{{ c.item_name }}</td>
                  <td data-label="描述">{{ c.description }}</td>
                  <td data-label="线索文本" class="clue-text">{{ c.clue_text }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </details>
      </div>

      <template v-if="catalogNotInPool.length > 0">
        <h6 class="block-title">图鉴中未入池（{{ catalogNotInPool.length }}）</h6>
        <p class="admin-hint">以下 Boss 已在图鉴中，但当前不在追查池随机范围内。</p>
        <div class="boss-list catalog-secondary">
          <details v-for="boss in catalogNotInPool" :key="boss.boss_id" class="boss-card">
            <summary class="boss-summary">
              <span class="boss-name">{{ boss.boss_name }}</span>
              <span class="boss-dungeon">🗺️ {{ boss.dungeon }}<template v-if="boss.dungeon_level_desc">（{{ boss.dungeon_level_desc }}）</template></span>
              <span :class="['status-pill', 'st-' + boss.status]">{{ statusLabel(boss.status) }}</span>
              <button
                v-if="boss.status === 'active'"
                type="button"
                class="btn-warning btn-sm release-lock-btn"
                :disabled="releasingBossId === boss.boss_id"
                @click.stop="onReleaseLock(boss)"
              >🔓 释放锁定</button>
            </summary>
            <div class="boss-body">
              <p class="boss-id muted small">ID：{{ boss.boss_id }}</p>
              <p v-if="boss.status === 'active'" class="lock-info muted small">
                锁定小队 #{{ boss.active_party_id ?? '—' }}
                <template v-if="boss.active_expedition_log_id"> · 探险日志 #{{ boss.active_expedition_log_id }}</template>
              </p>
              <div class="background-block">
                <strong>背景</strong>
                <p>{{ boss.background }}</p>
              </div>
              <table class="clue-table">
                <thead>
                  <tr>
                    <th>序号</th>
                    <th>物品名</th>
                    <th>描述</th>
                    <th>线索文本</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="c in boss.clues" :key="c.index">
                    <td data-label="序号">{{ c.index }}/4</td>
                    <td data-label="物品名">{{ c.item_name }}</td>
                    <td data-label="描述">{{ c.description }}</td>
                    <td data-label="线索文本" class="clue-text">{{ c.clue_text }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </details>
        </div>
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { useAdventurerS1Store } from '../stores/adventurerS1'

export interface ClueBureauBossClueDetail {
  index: number
  item_name: string
  description: string
  clue_text: string
}

export interface ClueBureauBossAdminDetail {
  boss_id: string
  dungeon: string
  dungeon_level_desc?: string | null
  boss_name: string
  background: string
  status: string
  clue_eligible?: boolean
  in_pool?: boolean
  active_party_id?: number | null
  active_expedition_log_id?: number | null
  clues: ClueBureauBossClueDetail[]
}

export interface ClueBureauPoolPreview {
  pool_count?: number
  pool_cap?: number
  grant_eligible_count?: number
  catalog_boss_count?: number
  need_count?: number
  would_add_count?: number
  can_supplement?: boolean
  supplement_block_reason?: 'pool_full' | 'ai_failed' | 'no_candidates' | null
  pool_bosses?: ClueBureauBossAdminDetail[]
  catalog_not_in_pool?: ClueBureauBossAdminDetail[]
}

const store = useAdventurerS1Store()
const message = useGlobalMessage()

const preview = ref<ClueBureauPoolPreview | null>(null)
const loading = ref(false)
const supplementing = ref(false)
const releasingBossId = ref<string | null>(null)

const poolBosses = computed(() => preview.value?.pool_bosses ?? [])
const catalogNotInPool = computed(() => preview.value?.catalog_not_in_pool ?? [])

const supplementBlockHint = computed(() => {
  const p = preview.value
  if (!p || p.can_supplement) return ''
  if (p.supplement_block_reason === 'pool_full') return '追查池已满，无需补充。'
  return '当前无法补充追查池。'
})

function statusLabel(status: string): string {
  const map: Record<string, string> = {
    available: '可追查',
    active: '讨伐中',
    defeated: '已讨伐',
  }
  return map[status] ?? status
}

async function loadPreview() {
  if (loading.value) return
  loading.value = true
  try {
    const res = await store.fetchClueBureauTracePoolPreview()
    if (res.code === 200) {
      preview.value = res.data ?? null
    } else {
      message.error(res.message || '加载追查池失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加载追查池失败')
  } finally {
    loading.value = false
  }
}

async function onSupplement() {
  if (supplementing.value) return
  if (!confirm('确认将线索追查池补充至上限？')) return
  supplementing.value = true
  try {
    const res = await store.adminSupplementClueBureauTracePool()
    if (res.code === 200) {
      if (res.data?.queued === true) {
        message.success('追查池补充任务已入队，后台生成需要时间，请稍后点击刷新查看结果。')
      } else {
        message.warning('未收到补充任务入队确认，请刷新追查池状态后重试。')
      }
      await loadPreview()
    } else {
      message.error(res.message || '补充失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '补充失败')
  } finally {
    supplementing.value = false
  }
}

async function onReleaseLock(boss: ClueBureauBossAdminDetail) {
  if (releasingBossId.value) return
  const label = boss.boss_name || boss.boss_id
  if (!confirm(`确认释放「${label}」的讨伐锁定？\n用于探险失败等遗留 active 状态。`)) return
  releasingBossId.value = boss.boss_id
  try {
    const res = await store.adminReleaseClueBureauBossLock(boss.boss_id)
    if (res.code === 200) {
      message.success(`已释放「${res.data?.boss_name || label}」锁定`)
      await loadPreview()
    } else {
      message.error(res.message || '释放失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '释放失败')
  } finally {
    releasingBossId.value = null
  }
}

onMounted(() => {
  void loadPreview()
})

defineExpose({ loadPreview })
</script>

<style scoped lang="scss">
.admin-clue-bureau {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
  max-width: 100%;
}

.admin-hint {
  font-size: 12px;
  color: var(--color-text-secondary, #888);
  margin: 0;
}

.admin-hint-warn {
  color: var(--color-warning, #b8860b);
}

.admin-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.pool-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.summary-stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 12px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  min-width: 100px;

  .stat-label {
    font-size: 11px;
    color: var(--color-text-secondary, #888);
  }

  strong {
    font-size: 16px;
  }
}

.block-title {
  font-size: 13px;
  font-weight: 600;
  margin: 12px 0 6px;
}

.empty-hint {
  font-size: 13px;
  color: var(--color-text-secondary, #888);
  padding: 8px 0;
}

.boss-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}

.boss-card {
  min-width: 0;
  max-width: 100%;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  overflow: hidden;
}

.boss-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  cursor: pointer;
  list-style: none;
  user-select: none;

  &::-webkit-details-marker {
    display: none;
  }

  &::before {
    content: '▸';
    margin-right: 4px;
    color: var(--color-text-secondary, #888);
    transition: transform 0.15s;
  }
}

.boss-card[open] > .boss-summary::before {
  transform: rotate(90deg);
}

.boss-name {
  font-weight: 600;
  font-size: 14px;
  overflow-wrap: anywhere;
}

.boss-dungeon {
  font-size: 13px;
  color: var(--color-text-secondary, #888);
  overflow-wrap: anywhere;
}

.release-lock-btn {
  margin-left: auto;
}

.lock-info {
  margin: 0 0 8px;
}

.status-pill {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--color-border);

  &.st-available {
    color: var(--color-success, #198754);
    border-color: var(--color-success, #198754);
  }

  &.st-active {
    color: var(--color-warning, #fd7e14);
    border-color: var(--color-warning, #fd7e14);
  }

  &.st-defeated {
    color: var(--color-text-secondary, #888);
  }
}

.eligible-pill {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(25, 135, 84, 0.12);
  color: var(--color-success, #198754);
}

.catalog-secondary .boss-card {
  opacity: 0.92;
}

.boss-body {
  min-width: 0;
  padding: 0 12px 12px;
  border-top: 1px solid var(--color-border);
}

.boss-id {
  margin: 8px 0 0;
}

.background-block {
  margin-top: 8px;
  font-size: 13px;
  line-height: 1.55;

  strong {
    display: block;
    margin-bottom: 4px;
    font-size: 12px;
    color: var(--color-text-secondary, #888);
  }

  p {
    margin: 0;
    white-space: pre-wrap;
  }
}

.clue-table {
  width: 100%;
  margin-top: 10px;
  border-collapse: collapse;
  font-size: 12px;

  th,
  td {
    border: 1px solid var(--color-border);
    padding: 6px 8px;
    text-align: left;
    vertical-align: top;
  }

  th {
    background: var(--color-surface, var(--color-bg));
    font-weight: 600;
  }

  .clue-text {
    max-width: 280px;
    white-space: pre-wrap;
  }
}

@media (max-width: 720px) {
  .pool-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .summary-stat { min-width: 0; padding: 8px; }
  .admin-actions { display: grid; grid-template-columns: 1fr; }
  .admin-actions button { width: 100%; min-height: 40px; }
  .boss-summary { align-items: flex-start; padding: 10px; }
  .release-lock-btn { width: 100%; margin-left: 0; }
  .boss-body { padding: 0 10px 10px; }
  .clue-table, .clue-table tbody, .clue-table tr, .clue-table td { display: block; width: 100%; box-sizing: border-box; }
  .clue-table thead { display: none; }
  .clue-table tr { margin-top: 8px; overflow: hidden; border: 1px solid var(--color-border); border-radius: 8px; }
  .clue-table td { display: grid; grid-template-columns: minmax(68px, auto) minmax(0, 1fr); gap: 8px; border: 0; border-bottom: 1px solid var(--color-border); }
  .clue-table td:last-child { border-bottom: 0; }
  .clue-table td::before { content: attr(data-label); color: var(--color-text-secondary, #888); font-weight: 600; }
  .clue-table .clue-text { max-width: none; overflow-wrap: anywhere; }
}

@media (max-width: 420px) {
  .pool-summary { grid-template-columns: 1fr; }
}

.muted {
  color: var(--color-text-secondary, #888);
}

.small {
  font-size: 11px;
}
</style>
