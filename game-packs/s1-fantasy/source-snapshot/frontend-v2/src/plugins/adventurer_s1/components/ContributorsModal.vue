<template>
  <Teleport to="body">
  <div class="modal-mask contributors-mask s1-mobile-surface s1-mobile-overlay" @click.self="$emit('close')">
    <div class="modal-box modal-contributors s1-mobile-dialog">
      <header class="contributors-head">
        <h4 class="modal-title">📊 贡献者排行</h4>
        <button type="button" class="contributors-close" aria-label="关闭贡献者排行" @click="$emit('close')">✕</button>
      </header>
      <div v-if="loading" class="empty-helpers">加载中…</div>
      <template v-else-if="contributors">
        <!-- 综合排行：按玩家归并，工时占比×50% + 材料占比×50% -->
        <div class="combined-section">
          <h5 class="col-title">🏆 综合贡献（时长 50% + 材料 50%）</h5>
          <div v-if="!contributors.combined_contributors?.length" class="empty-helpers">暂无记录</div>
          <div v-else class="contributor-list">
            <div
              v-for="(c, i) in contributors.combined_contributors"
              :key="'cc-' + (c.user_id ?? 'npc-' + i)"
              class="contributor-row"
            >
              <span class="contributor-rank" :class="rankClass(i)">{{ i + 1 }}</span>
              <span class="contributor-name">
                {{ c.display_name }}<span
                  v-if="i === 0 && c.user_id !== null && showNamingCrown"
                  class="naming-crown"
                  title="最大贡献者：获得这栋楼的命名权"
                >👑</span>
              </span>
              <span class="combined-detail">🏗️{{ c.effective_hours }}h · ⭐{{ c.material_amount }}</span>
              <span class="contributor-value">{{ pct(c.score) }}</span>
            </div>
          </div>
        </div>
        <div class="contributors-columns">
          <div class="contributors-col">
            <h5 class="col-title">🏗️ 有效帮忙时长</h5>
            <div v-if="contributors.time_contributors.length === 0" class="empty-helpers">暂无记录</div>
            <div v-else class="contributor-list">
              <div v-for="(t, i) in contributors.time_contributors" :key="'tc-' + t.adventurer_id" class="contributor-row">
                <span class="contributor-rank" :class="rankClass(i)">{{ i + 1 }}</span>
                <span class="contributor-name">{{ t.adventurer_name }}</span>
                <span class="contributor-value">{{ t.effective_hours }}h</span>
              </div>
            </div>
          </div>
          <div class="contributors-col">
            <h5 class="col-title">🪵 材料捐献</h5>
            <div v-if="contributors.material_contributors.length === 0" class="empty-helpers">暂无记录</div>
            <div v-else class="contributor-list">
              <div v-for="(m, i) in contributors.material_contributors" :key="'mc-' + (m.adventurer_id ?? 'u' + m.user_id)" class="contributor-row">
                <span class="contributor-rank" :class="rankClass(i)">{{ i + 1 }}</span>
                <span class="contributor-name">{{ m.adventurer_name }}</span>
                <span class="contributor-materials">
                  <span v-for="(amount, mat) in m.materials" :key="mat" class="mat-tag">{{ matIcon(String(mat)) }}{{ amount }}</span>
                </span>
                <span class="contributor-value">共{{ m.total_amount }}贡献</span>
              </div>
            </div>
          </div>
        </div>
      </template>
      <div class="modal-actions s1-mobile-dialog-footer">
        <button type="button" class="btn-cancel" @click="$emit('close')">关闭</button>
      </div>
    </div>
  </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useAdventurerS1Store, type BuildingContributors } from '../stores/adventurerS1'

const props = withDefaults(defineProps<{
  buildingId: number
  // 综合贡献第一名是否显示命名权皇冠（仅宿舍楼有命名权）
  showNamingCrown?: boolean
}>(), {
  showNamingCrown: false,
})

defineEmits<{
  close: []
}>()

const store = useAdventurerS1Store()
const loading = ref(false)
const contributors = ref<BuildingContributors | null>(null)

onMounted(async () => {
  loading.value = true
  try {
    contributors.value = await store.fetchBuildingContributors(props.buildingId)
  } finally {
    loading.value = false
  }
})

function rankClass(i: number): string {
  if (i === 0) return 'rank-gold'
  if (i === 1) return 'rank-silver'
  if (i === 2) return 'rank-bronze'
  return ''
}

function pct(v: number): string {
  return (v * 100).toFixed(1) + '%'
}

function matIcon(name: string): string {
  const map: Record<string, string> = {
    wood: '🪵',
    stone: '🪨',
    iron_ingot: '🔩',
    charcoal: '⚫',
  }
  return map[name] ?? '📦'
}
</script>

<style scoped>
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1550;
  padding: 16px;
}
.modal-box {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 24px;
  width: 95%;
  max-height: min(85dvh, 760px);
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
  box-sizing: border-box;
}
.modal-contributors {
  max-width: 640px;
}
.modal-title {
  margin: 0;
  font-size: 16px;
  color: var(--color-text);
}
.contributors-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
.contributors-close {
  flex: none;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
}
.contributors-close:hover { background: var(--color-bg); color: var(--color-text); }
.combined-section {
  margin-bottom: 14px;
}
.contributors-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}
@media (max-width: 560px) {
  .contributors-columns {
    grid-template-columns: 1fr;
  }

  .modal-box {
    padding: 16px;
  }
}
.contributors-col {
  min-width: 0;
}
.col-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  margin: 0 0 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--color-border);
}
.contributor-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.contributor-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  border-radius: 6px;
  font-size: 13px;
  color: var(--color-text);
  background: var(--color-bg);
  min-width: 0;
}
.contributor-rank {
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  font-size: 11px;
  font-weight: 700;
  background: var(--color-border);
  color: var(--color-text-secondary);
  flex-shrink: 0;
}
.contributor-rank.rank-gold {
  background: #fbbf24;
  color: #000;
}
.contributor-rank.rank-silver {
  background: #9ca3af;
  color: #000;
}
.contributor-rank.rank-bronze {
  background: #d97706;
  color: #fff;
}
.contributor-name {
  font-weight: 500;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.naming-crown {
  margin-left: 4px;
}
.combined-detail {
  font-size: 11px;
  color: var(--color-text-secondary);
  white-space: nowrap;
  flex-shrink: 0;
}
.contributor-materials {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}
.mat-tag {
  font-size: 11px;
  white-space: nowrap;
}
.contributor-value {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
  white-space: nowrap;
  flex-shrink: 0;
}
.empty-helpers {
  text-align: center;
  padding: 12px;
  color: var(--color-text-secondary);
  font-size: 13px;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
.btn-cancel {
  padding: 8px 16px;
  font-size: 13px;
  background: var(--color-bg);
  color: var(--color-text);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  cursor: pointer;
}
.btn-cancel:hover {
  border-color: var(--color-primary);
}

@media (max-width: 560px) {
  .modal-mask {
    align-items: flex-end;
    padding: 0;
  }

  .modal-box {
    width: 100%;
    max-height: 92dvh;
    padding: 16px 14px max(16px, env(safe-area-inset-bottom));
    border-right: 0;
    border-bottom: 0;
    border-left: 0;
    border-radius: 16px 16px 0 0;
    scrollbar-gutter: auto;
  }

  .contributor-row {
    display: grid;
    grid-template-columns: 22px minmax(0, 1fr) auto;
    gap: 5px 7px;
    padding: 8px;
  }

  .combined-detail,
  .contributor-materials {
    grid-column: 2 / -1;
    white-space: normal;
  }

  .contributor-value {
    grid-column: 3;
    grid-row: 1;
  }
}
</style>
