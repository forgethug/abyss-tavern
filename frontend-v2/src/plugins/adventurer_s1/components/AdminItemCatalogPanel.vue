<template>
  <section class="advs1-card">
    <div class="panel-header">
      <h3 class="col-title">📚 {{ isReadonly ? '物品图鉴' : '物品图鉴管理' }}</h3>
      <div v-if="!isReadonly" style="display: flex; gap: 8px; flex-wrap: wrap;">
        <button type="button" class="btn-secondary" :disabled="repairingTypes" @click="repairToolTypes">
          {{ repairingTypes ? '修复中…' : '🗺️ 修复误标工具类型' }}
        </button>
        <button type="button" class="btn-secondary" :disabled="restoringTypes" @click="restoreToolTypes">
          {{ restoringTypes ? '还原中…' : '↩️ 还原类型' }}
        </button>
        <button type="button" class="btn-secondary" :disabled="repairingRarity" @click="repairUncommonRarity">
          {{ repairingRarity ? '修复中…' : '✨ 修复 uncommon 稀有度' }}
        </button>
        <button type="button" class="btn-secondary" :disabled="restoringRarity" @click="restoreUncommonRarity">
          {{ restoringRarity ? '还原中…' : '↩️ 还原稀有度' }}
        </button>
        <button type="button" class="btn-secondary" :disabled="repairingEvolutionStages" @click="repairCompanionEvolutionStages">
          {{ repairingEvolutionStages ? '修复中…' : '🐉 修复嬗变阶位' }}
        </button>
        <button type="button" class="btn-secondary" :disabled="restoringEvolutionStages" @click="restoreCompanionEvolutionStages">
          {{ restoringEvolutionStages ? '还原中…' : '↩️ 还原嬗变阶位' }}
        </button>
        <button type="button" class="btn-secondary" :disabled="reclaimingEvolutionFees" @click="reclaimCompanionEvolutionFees">
          {{ reclaimingEvolutionFees ? '回收中…' : '💰 预览并回收漏缴嬗变费' }}
        </button>
        <button type="button" class="btn-secondary" :disabled="reconcilingEvolutionFees" @click="reconcileCompanionEvolutionFees">
          {{ reconcilingEvolutionFees
            ? (companionIntegrityScanProgress || '校准中…')
            : '⚖️ 检查费用、阶级与伙伴谱系' }}
        </button>
        <button
          type="button"
          class="btn-secondary"
          :disabled="restoringCompanionContinuity || !lastCompanionContinuityRunId"
          @click="restoreCompanionContinuityRepair"
        >
          {{ restoringCompanionContinuity ? '移除中…' : '↩️ 移除上次伙伴谱系绑定' }}
        </button>
        <button type="button" class="advs1-submit" @click="openCreate">+ 新增物品</button>
      </div>
    </div>

    <!-- 搜索筛选 -->
    <div class="filter-bar">
      <input
        v-model="search"
        type="text"
        placeholder="搜索物品名称…"
        @input="debouncedFetch"
      >
      <select v-model="typeFilter" @change="fetchList">
        <option value="">全部类型</option>
        <option v-for="t in ITEM_TYPE_OPTIONS" :key="t.value" :value="t.value">{{ t.label }}</option>
      </select>
      <select v-model="rarityFilter" @change="fetchList">
        <option value="">全部稀有度</option>
        <option value="common">普通</option>
        <option value="rare">稀有</option>
        <option value="epic">史诗</option>
        <option value="legendary">传说</option>
      </select>
    </div>

    <div v-if="loading" class="advs1-state">加载中…</div>
    <div v-else-if="items.length === 0" class="advs1-state">暂无匹配物品</div>
    <div v-else class="catalog-list">
      <div v-for="item in items" :key="item.id" class="catalog-item">
        <div class="catalog-item-head">
          <span class="catalog-item-name" :style="{ color: rarityColor(item.rarity) }">
            {{ item.name }}
          </span>
          <span :class="'rarity-tag r-' + item.rarity">{{ rarityLabel(item.rarity) }}</span>
          <span v-if="item.type" class="catalog-item-type">{{ itemTypeLabel(item.type) }}</span>
          <span v-if="item.world_unique" class="badge badge-unique">全服唯一</span>
          <span v-if="item.is_starter" class="badge badge-starter">初始可选</span>
          <span v-if="item.use_condition?.hidden_boss_dungeon" class="badge badge-dungeon">
            📍{{ item.use_condition.hidden_boss_dungeon }}
            <template v-if="item.use_condition?.clue_index"> · 线索 {{ item.use_condition.clue_index }}/4</template>
          </span>
          <span v-else-if="item.use_condition?.expedition_dungeon" class="badge badge-dungeon">
            📍{{ item.use_condition.expedition_dungeon }}{{ item.use_condition.target_class ? `·${item.use_condition.target_class}` : '' }}
          </span>
          <span v-if="item.text_locked || item.is_hidden_boss_clue" class="badge badge-locked">🔒 文案锁定</span>
          <span v-if="itemDefaultUsesLeft(item) != null" class="badge badge-uses">
            🔢 {{ itemDefaultUsesLeft(item) }} 次
          </span>
        </div>
        <p v-if="item.description" class="catalog-item-desc">{{ item.description }}</p>
        <div v-if="!isReadonly" class="catalog-item-actions">
          <button type="button" class="btn-secondary btn-sm" @click="openEdit(item)">编辑</button>
          <button
            v-if="!item.is_hidden_boss_clue"
            type="button"
            class="btn-secondary btn-sm"
            :disabled="historyBusy && historyItem?.id === item.id"
            @click="openHistorySync(item)"
          >
            {{ historyBusy && historyItem?.id === item.id ? '处理中…' : '🕰️ 历史同步' }}
          </button>
          <button
            v-if="!item.is_hidden_boss_clue"
            type="button"
            class="btn-danger btn-sm"
            :disabled="deletingId === item.id"
            @click="confirmDelete(item)"
          >
            {{ deletingId === item.id ? '删除中…' : '删除' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 新增/编辑弹窗 -->
    <Teleport to="body">
      <div v-if="showModal" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="closeModal">
        <div class="modal-container s1-mobile-dialog">
          <div class="modal-header s1-mobile-dialog-header">
            <h4>{{ editingItem ? '✏️ 编辑物品' : '📚 新增物品' }}</h4>
            <button type="button" class="btn-close" @click="closeModal">&times;</button>
          </div>
          <div class="modal-body s1-mobile-dialog-body">
            <p v-if="isEditingLocked" class="locked-hint">
              🔒 追查线索：名称/描述/副本条件由线索追查处图鉴同步，此处不可改。
              <template v-if="editingItem?.use_condition?.hidden_boss_dungeon">
                限定副本：{{ editingItem.use_condition.hidden_boss_dungeon }}
                <template v-if="editingItem.use_condition?.clue_index">
                  · 线索 {{ editingItem.use_condition.clue_index }}/4
                </template>
              </template>
            </p>
            <label class="form-label">
              物品名称
              <input v-model="form.name" type="text" class="advs1-input" placeholder="输入物品名称" maxlength="100" :disabled="isEditingLocked" />
            </label>
            <label class="form-label">
              描述
              <textarea v-model="form.description" class="advs1-input" rows="3" placeholder="物品描述" :disabled="isEditingLocked"></textarea>
            </label>
            <div class="form-row">
              <label class="form-label">
                稀有度
                <select v-model="form.rarity" class="advs1-input" :disabled="isEditingLocked">
                  <option value="common">普通</option>
                  <option value="rare">稀有</option>
                  <option value="epic">史诗</option>
                  <option value="legendary">传说</option>
                </select>
              </label>
              <label class="form-label">
                类型
                <select v-model="form.type" class="advs1-input" :disabled="isEditingLocked">
                  <option v-for="t in ITEM_TYPE_OPTIONS" :key="t.value" :value="t.value">{{ t.label }}</option>
                </select>
              </label>
            </div>
            <label class="form-label checkbox-label">
              <input v-model="form.world_unique" type="checkbox" :disabled="isEditingLocked" />
              全服唯一（只有一件存在）
            </label>
            <label class="form-label checkbox-label">
              <input v-model="form.is_starter" type="checkbox" :disabled="isEditingLocked" />
              初始可选（创建冒险者时可选）
            </label>
            <AdminItemUsesLeftField v-model="form.uses_left" :disabled="isEditingLocked" />
            <label class="form-label checkbox-label">
              <input v-model="form.text_locked" type="checkbox" :disabled="isEditingLocked" />
              不可变（禁止申诉/炼金/嬗变修改，探险剧情仍可消耗）
            </label>

            <div class="uc-section">
              <label class="form-label checkbox-label">
                <input v-model="form.uc_enabled" type="checkbox" :disabled="isEditingLocked" />
                副本限定（仅在特定地牢中对 AI 可见）
              </label>
              <div v-if="form.uc_enabled" class="uc-fields">
                <label class="form-label">
                  限定副本
                  <select v-model="form.uc_dungeon" class="advs1-input" :disabled="isEditingLocked">
                    <option value="">请选择副本…</option>
                    <option v-for="d in dungeonList" :key="d" :value="d">{{ d }}</option>
                  </select>
                </label>
                <label class="form-label">
                  目标职业（可选，如转职邀请函填职业名）
                  <input v-model="form.uc_target_class" type="text" class="advs1-input" placeholder="如：刺客、骑士" maxlength="20" :disabled="isEditingLocked" />
                </label>
              </div>
            </div>
          </div>
          <div class="modal-footer s1-mobile-dialog-footer">
            <button type="button" class="btn-secondary" @click="closeModal">取消</button>
            <button
              type="button"
              class="advs1-submit"
              :disabled="!form.name.trim() || submitting || isEditingLocked"
              @click="doSubmit"
            >
              {{ submitting ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- 旧图鉴值 → 当前资产的显式历史修复 -->
    <Teleport to="body">
      <div v-if="showHistoryModal" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="closeHistorySync">
        <div class="modal-container history-sync-modal s1-mobile-dialog">
          <div class="modal-header s1-mobile-dialog-header">
            <h4>🕰️ 修复「{{ historyDisplayedCatalog?.name || historyItem?.name }}」的历史实例</h4>
            <button type="button" class="btn-close" :disabled="historyBusy" @click="closeHistorySync">&times;</button>
          </div>
          <div class="modal-body s1-mobile-dialog-body">
            <p class="history-safety">
              填写这条图鉴修改前的完整标准值。系统只命中名称、类型、稀有度及描述都仍与旧图鉴一致的当前资产；
              玩家自定义描述和已完成、取消、拒绝、结算的市场历史不会修改。不可变由当前图鉴按物品名实时生效，
              旧名实例同步为当前名称后会同时继承当前不可变设置。
            </p>

            <div v-if="historyDisplayedCatalog" class="history-current">
              <strong>当前图鉴</strong>
              <span>
                {{ historyDisplayedCatalog.name }} ·
                {{ historyDisplayedCatalog.type }} ·
                {{ historyDisplayedCatalog.rarity }} ·
                {{ historyDisplayedImmutable ? '不可变' : '可变' }}
              </span>
              <p>{{ historyDisplayedCatalog.description || '（空描述）' }}</p>
            </div>
            <p v-if="historyCatalogChangedSinceOpen" class="history-notice">
              图鉴在弹窗打开后发生过变化；以上内容来自本次后端预览，是执行时采用的当前权威值。
            </p>

            <fieldset class="history-form" :disabled="historyBusy">
              <div class="form-row">
                <label class="form-label">
                  修改前名称
                  <input v-model="historyForm.name" type="text" class="advs1-input" maxlength="100" />
                </label>
                <label class="form-label">
                  修改前类型代码
                  <input v-model="historyForm.type" type="text" class="advs1-input" placeholder="如 weapon" maxlength="50" />
                </label>
                <label class="form-label">
                  修改前稀有度代码
                  <input v-model="historyForm.rarity" type="text" class="advs1-input" placeholder="如 rare" maxlength="30" />
                </label>
              </div>
              <label class="form-label">
                修改前描述
                <textarea v-model="historyForm.description" class="advs1-input" rows="3" placeholder="允许为空"></textarea>
              </label>
              <label class="form-label checkbox-label">
                <input v-model="historyForm.is_immutable" type="checkbox" />
                修改前图鉴为不可变
              </label>
              <label class="form-label checkbox-label">
                <input v-model="historyForm.has_default_uses_left" type="checkbox" />
                我确认修改前图鉴设置过默认使用次数
              </label>
              <label v-if="historyForm.has_default_uses_left" class="form-label">
                修改前默认使用次数
                <input
                  v-model.number="historyForm.default_uses_left"
                  type="number"
                  class="advs1-input"
                  min="0"
                  step="1"
                />
                <small>只同步剩余次数仍等于这个旧默认值的实例；已消耗次数保持不变。</small>
              </label>
              <label class="form-label">
                操作备注（可选）
                <input v-model="historyForm.note" type="text" class="advs1-input" maxlength="200" />
              </label>
            </fieldset>

            <template v-if="historyPreview">
              <div
                v-if="historyPreview.blocked_reason"
                class="history-blocked"
              >
                {{ historyPreview.blocked_reason }}
              </div>
              <div v-else-if="historyPreview.counts.total === 0" class="history-empty">
                <template v-if="historyPreview.old_snapshot.is_immutable !== historyPreview.catalog_item.is_immutable">
                  不可变已由当前图鉴实时应用到同名实例，无需逐条写入；没有找到还需同步名称或标准属性的当前资产。
                </template>
                <template v-else>
                  没有找到需要修复的当前资产。请核对修改前的完整值，或确认这些实例已经同步。
                </template>
              </div>

              <div class="history-counts">
                <div
                  v-for="row in historyCountRows"
                  :key="row.key"
                  class="history-count-card"
                >
                  <span>{{ row.label }}</span>
                  <strong>{{ row.count }}</strong>
                </div>
              </div>

              <p class="history-summary">
                共命中 {{ historyPreview.counts.total }} 条待修复记录；
                精确扫描到 {{ historyPreview.scan.exact_old_rows }} 条旧值记录，
                其中 {{ historyPreview.scan.already_current_rows }} 条无需改动。
                历史市场记录修改数固定为 {{ historyPreview.historical_market_rows_updated }}。
              </p>

              <details v-if="historyPreview.samples.length" class="history-samples">
                <summary>查看最多 {{ historyPreview.samples.length }} 条命中样本</summary>
                <div
                  v-for="sample in historyPreview.samples"
                  :key="`${sample.target}-${sample.target_id}`"
                  class="history-sample"
                >
                  <strong>{{ historyTargetLabel(sample.target) }} #{{ sample.target_id }}</strong>
                  <span>
                    {{ sample.before.name }} / {{ sample.before.type }} / {{ sample.before.rarity }}
                    →
                    {{ sample.after.name }} / {{ sample.after.type }} / {{ sample.after.rarity }}
                  </span>
                  <span v-if="sample.immutable_changed">
                    不可变：{{ sample.before.is_immutable ? '是' : '否' }}
                    → {{ sample.after.is_immutable ? '是' : '否' }}
                  </span>
                  <small>
                    {{ sample.content_changed ? '同步标准属性' : '' }}
                    {{ sample.content_changed && sample.default_uses_changed ? '；' : '' }}
                    {{ sample.default_uses_changed ? '同步未消耗默认次数' : '' }}
                  </small>
                </div>
              </details>

              <div v-if="historyPreview.latest_backup_summary" class="history-backup">
                <strong>最近可还原批次</strong>
                <span>
                  {{ formatHistoryTimestamp(historyPreview.latest_backup_summary.created_at) }}
                  · 共 {{ historyPreview.latest_backup_summary.repaired.total }} 条
                  · 尚待安全还原 {{ historyPreview.latest_backup_summary.remaining_entries }} 条
                </span>
                <span v-if="historyPreview.latest_backup_summary.status === 'partially_restored'">
                  该批次此前只完成了部分还原；其余记录仍需满足修复后状态才会恢复。
                </span>
                <span v-if="historyPreview.latest_backup_summary.old_snapshot && historyPreview.latest_backup_summary.current_snapshot">
                  {{ historyPreview.latest_backup_summary.old_snapshot.name }}
                  →
                  {{ historyPreview.latest_backup_summary.current_snapshot.name }}
                </span>
                <span v-if="historyPreview.latest_backup_summary.note">
                  备注：{{ historyPreview.latest_backup_summary.note }}
                </span>
                <small>批次：{{ historyPreview.latest_backup_summary.run_id }}</small>
              </div>
            </template>

            <p v-if="historyReceipt" class="history-receipt">{{ historyReceipt }}</p>
          </div>
          <div class="modal-footer history-footer s1-mobile-dialog-footer">
            <button type="button" class="btn-secondary" :disabled="historyBusy" @click="closeHistorySync">
              关闭
            </button>
            <button
              v-if="historyPreview?.latest_backup_run_id"
              type="button"
              class="btn-secondary"
              :disabled="historyBusy"
              @click="restoreHistorySync"
            >
              {{ historyRestoring ? '还原中…' : '↩️ 还原最近批次' }}
            </button>
            <button type="button" class="btn-secondary" :disabled="historyBusy" @click="previewHistorySync()">
              {{ historyPreviewing ? '预览中…' : '预览命中范围' }}
            </button>
            <button
              type="button"
              class="advs1-submit"
              :disabled="!canExecuteHistorySync"
              @click="executeHistorySync"
            >
              {{ historyExecuting ? '修复中…' : '确认执行历史修复' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<script setup lang="ts">
import { tavernStorageKey } from '@/runtime/activeTavern'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import {
  useAdventurerS1Store,
  type CatalogHistorySyncPreview,
  type CatalogHistorySyncSample,
  type CatalogHistorySyncOldSnapshotInput,
} from '../stores/adventurerS1'
import { rarityColor, rarityLabel, itemTypeLabel, ITEM_TYPE_OPTIONS, S1_DEFAULT_ITEM_TYPE } from '../utils'
import {
  buildCatalogUseCondition,
  parseCatalogUseConditionToForm,
  appendUsesLeftPayload,
  extractDefaultUsesLeft,
  type CatalogUseConditionFormSlice,
} from '../composables/adminItemForm'
import AdminItemUsesLeftField from './AdminItemUsesLeftField.vue'

const store = useAdventurerS1Store()
const message = useMessage()
const props = withDefaults(defineProps<{
  readonly?: boolean
}>(), {
  readonly: false,
})
const isReadonly = computed(() => props.readonly)

// ── 误标 consumable 的可复用工具修复（图鉴 + 实例 type 一起改 document）──────────
const repairingTypes = ref(false)
async function repairToolTypes() {
  if (repairingTypes.value) return
  // 先 dry-run 预览候选名单，交管理员核对再二次确认
  let preview: any
  try {
    const res = await store.adminPreviewRepairToolTypes()
    if (res.code !== 200) { message.error(res.message || '预览失败'); return }
    preview = res.data
  } catch (e: any) {
    message.error(e?.response?.data?.message || '预览失败'); return
  }
  const total = preview?.total ?? 0
  if (total === 0) {
    message.info('没有发现被误标为消耗品的可复用工具')
    return
  }
  const names: string[] = (preview?.candidates ?? []).map((c: any) => c.name)
  const sample = names.slice(0, 15).join('、') + (names.length > 15 ? ` 等${names.length}项` : '')
  const instTotal = preview?.instance_total ?? 0
  if (!confirm(
    `将把以下 ${total} 种被误标为「消耗品」的可复用工具改为「${preview?.target_type || 'document'}」类型，`
    + `连同玩家持有的 ${instTotal} 件实例一并修正（避免它们在探险中被当一次性消耗删除）：\n\n`
    + sample
    + '\n\n修复前会自动备份原类型，出问题可点「↩️ 还原类型」一键撤销。确定吗？'
  )) return
  repairingTypes.value = true
  try {
    const res = await store.adminRepairToolTypes()
    if (res.code === 200) {
      message.success(
        `修复完成：图鉴 ${res.data?.repaired ?? 0} 种、实例 ${res.data?.instances_updated ?? 0} 件已改为 document，已备份`,
      )
      fetchList()
    } else {
      message.error(res.message || '修复失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '修复失败')
  } finally {
    repairingTypes.value = false
  }
}

const restoringTypes = ref(false)
async function restoreToolTypes() {
  if (restoringTypes.value) return
  if (!confirm('从最近一次物品类型修复备份还原（撤销上次修复）。确定吗？')) return
  restoringTypes.value = true
  try {
    const res = await store.adminRestoreToolTypes()
    if (res.code === 200) {
      message.success(`已还原 ${res.data?.restored ?? 0} 行物品类型`)
      fetchList()
    } else {
      message.error(res.message || '还原失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '还原失败')
  } finally {
    restoringTypes.value = false
  }
}

const repairingRarity = ref(false)
async function repairUncommonRarity() {
  if (repairingRarity.value) return
  let preview: any
  try {
    const res = await store.adminPreviewRepairUncommonRarity()
    if (res.code !== 200) { message.error(res.message || '预览失败'); return }
    preview = res.data
  } catch (e: any) {
    message.error(e?.response?.data?.message || '预览失败'); return
  }
  const total = preview?.total ?? 0
  if (total === 0) {
    message.info('没有发现已入库的 uncommon 稀有度数据')
    return
  }
  const names: string[] = preview?.catalog_names ?? []
  const sample = names.slice(0, 12).join('、') + (names.length > 12 ? ` 等${names.length}项` : '')
  const breakdown = preview?.breakdown ?? {}
  const detail = [
    breakdown.catalog ? `图鉴 ${breakdown.catalog}` : '',
    breakdown.equipment ? `随身 ${breakdown.equipment}` : '',
    breakdown.warehouse ? `仓库 ${breakdown.warehouse}` : '',
    breakdown.item_logs ? `流水 ${breakdown.item_logs}` : '',
  ].filter(Boolean).join('，')
  if (!confirm(
    `将把已入库的 uncommon 稀有度统一改为「${preview?.target_rarity || 'rare'}」（共 ${total} 行`
    + (detail ? `：${detail}` : '')
    + '）。\n\n'
    + (sample ? `图鉴示例：${sample}\n\n` : '')
    + '修复前会自动备份，出问题可点「↩️ 还原稀有度」撤销。确定吗？'
  )) return
  repairingRarity.value = true
  try {
    const res = await store.adminRepairUncommonRarity()
    if (res.code === 200) {
      message.success(`修复完成：共 ${res.data?.repaired ?? 0} 行已改为 rare，已备份`)
      fetchList()
    } else {
      message.error(res.message || '修复失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '修复失败')
  } finally {
    repairingRarity.value = false
  }
}

const restoringRarity = ref(false)
async function restoreUncommonRarity() {
  if (restoringRarity.value) return
  if (!confirm('从最近一次 uncommon 稀有度修复备份还原。确定吗？')) return
  restoringRarity.value = true
  try {
    const res = await store.adminRestoreUncommonRarity()
    if (res.code === 200) {
      message.success(`已还原 ${res.data?.restored ?? 0} 行稀有度`)
      fetchList()
    } else {
      message.error(res.message || '还原失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '还原失败')
  } finally {
    restoringRarity.value = false
  }
}

const repairingEvolutionStages = ref(false)
async function repairCompanionEvolutionStages() {
  if (repairingEvolutionStages.value) return
  let preview: any
  try {
    const res = await store.adminPreviewRepairCompanionEvolutionStages()
    if (res.code !== 200) { message.error(res.message || '预览失败'); return }
    preview = res.data
  } catch (e: any) {
    message.error(e?.response?.data?.message || '预览失败'); return
  }
  const candidates: any[] = preview?.candidates ?? []
  if (candidates.length === 0) {
    message.info('没有发现低于成功嬗变日志的唯一匹配伙伴')
    return
  }
  const sample = candidates.slice(0, 12).map((item) => (
    `${item.adventurer_name || `#${item.adventurer_id}`}·${item.item_name}：`
    + `${item.current_stage}阶→${item.target_stage}阶`
  )).join('\n')
  const omitted = candidates.length > 12 ? `\n……另有 ${candidates.length - 12} 项` : ''
  const ambiguous = preview?.ambiguous ?? 0
  if (!confirm(
    `将按成功嬗变日志恢复 ${candidates.length} 个伙伴的阶位，只升不降。\n\n`
    + sample + omitted
    + (ambiguous ? `\n\n另有 ${ambiguous} 组同名多实例因无法确认身份，将跳过。` : '')
    + '\n\n修复前会自动备份，可用“还原嬗变阶位”撤销。确定吗？'
  )) return
  repairingEvolutionStages.value = true
  try {
    const res = await store.adminRepairCompanionEvolutionStages()
    if (res.code === 200) {
      message.success(`已恢复 ${res.data?.repaired ?? 0} 个伙伴的嬗变阶位`)
    } else {
      message.error(res.message || '修复失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '修复失败')
  } finally {
    repairingEvolutionStages.value = false
  }
}

const restoringEvolutionStages = ref(false)
async function restoreCompanionEvolutionStages() {
  if (restoringEvolutionStages.value) return
  if (!confirm('从最近一次伙伴嬗变阶位修复备份还原。确定吗？')) return
  restoringEvolutionStages.value = true
  try {
    const res = await store.adminRestoreCompanionEvolutionStages()
    if (res.code === 200) {
      message.success(`已还原 ${res.data?.restored ?? 0} 个伙伴的嬗变阶位`)
    } else {
      message.error(res.message || '还原失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '还原失败')
  } finally {
    restoringEvolutionStages.value = false
  }
}

const formatGold = (value: unknown) => Number(value || 0).toFixed(2)
const companionContinuityRiskLabels: Record<string, string> = {
  continuity_binding_missing: '缺少已审核的旧伙伴→新形态绑定',
  continuity_binding_ambiguous: '同一旧伙伴存在多条审核绑定',
  reviewed_survivor_missing: '已审核的新形态不在最终新增物品中',
  reviewed_survivor_type_mismatch: '已审核的新形态不再是伙伴类型',
  reviewed_survivor_stage_regression: '已审核的新形态阶级低于冻结下限',
  reviewed_survivor_description_not_verbatim_preserved: '新描述未原样保留冻结旧描述',
  single_new_item_not_companion: '同轮唯一新物品不是伙伴类型',
  single_new_companion_stage_below_departure: '同轮唯一新伙伴阶级低于出发阶级',
  single_new_item_missing_frozen_description: '同轮唯一新物品未原样保留冻结旧描述',
  new_item_mapping_ambiguous: '同轮有多个新物品，无法机械配对',
}

function formatCompanionContinuityRisk(item: any) {
  const newItems = (item?.new_items ?? []).map((newItem: any) => (
    `${newItem.name || '未命名'}`
    + `(${newItem.type || '未知类型'}, ${newItem.evolution_stage ?? 0}阶, `
    + `描述${newItem.description_length ?? 0}字)`
  )).join('、') || '无可列出的同轮新物品'
  const newItemsOverflow = item?.new_items_truncated
    ? `（另有 ${Math.max(0, Number(item?.new_item_count || 0) - 20)} 件未展开）`
    : ''
  const riskText = (item?.risk_codes ?? []).map(
    (code: string) => companionContinuityRiskLabels[code] || code,
  ).join('；')
  return (
    `探险#${item.expedition_log_id}·`
    + `${item.adventurer_name || `#${item.adventurer_id}`}：`
    + `冻结伙伴「${item.consumed_name}」${item.departure_stage ?? 0}阶`
    + `（旧描述${item.departure_description_length ?? 0}字）；`
    + `同轮新物：${newItems}${newItemsOverflow}；风险：${riskText}`
  )
}

const reclaimingEvolutionFees = ref(false)
async function reclaimCompanionEvolutionFees() {
  if (reclaimingEvolutionFees.value) return
  let preview: any
  try {
    const res = await store.adminPreviewCompanionEvolutionFeeReclaims()
    if (res.code !== 200) { message.error(res.message || '预览失败'); return }
    preview = res.data
  } catch (e: any) {
    message.error(e?.response?.data?.message || '预览失败'); return
  }
  const items: any[] = preview?.items ?? []
  const summary = preview?.summary ?? {}
  if (items.length === 0) {
    message.info(
      `没有待回收的唯一连续嬗变链。已处理 ${summary.already_processed ?? 0} 项，`
      + `已由校准账本对平 ${summary.already_balanced_by_reconciliation ?? 0} 项，`
      + `歧义跳过 ${summary.ambiguous_skipped ?? 0} 项。`
    )
    return
  }
  const sample = items.slice(0, 12).map((item) => (
    `${item.adventurer_name || `#${item.adventurer_id}`}·${item.item_name}：`
    + `${item.before?.actual_stage ?? 0}阶按${item.after?.expected_stage ?? 0}阶计费，`
    + `正确差价 ${formatGold(item.correct_gold_due)}，已净扣 ${formatGold(item.already_collected)}，`
    + `本次补扣 ${formatGold(item.gold_to_deduct)} 金币`
  )).join('\n')
  const omitted = items.length > 12 ? `\n……另有 ${items.length - 12} 项` : ''
  if (!confirm(
    `发现 ${summary.candidate_count ?? items.length} 次可唯一确认的漏缴情形，`
    + `涉及 ${summary.adventurer_count ?? 0} 名冒险者，合计 ${summary.gold_to_deduct ?? 0} 金币。\n\n`
    + sample + omitted
    + `\n\n${summary.calculation_basis || ''}`
    + `\n歧义链已跳过 ${summary.ambiguous_skipped ?? 0} 项。`
    + '\n扣款幂等并写入异常收益回收台账；余额不足时允许变为负数。确定执行吗？'
  )) return
  reclaimingEvolutionFees.value = true
  try {
    const res = await store.adminExecuteCompanionEvolutionFeeReclaims(
      String(preview.preview_token || ''),
      '伙伴嬗变阶位回退漏缴费用回收',
    )
    if (res.code === 200) {
      message.success(`已回收 ${res.data?.summary?.gold_to_deduct ?? summary.gold_to_deduct ?? 0} 金币`)
    } else {
      message.error(res.message || '回收失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '回收失败')
  } finally {
    reclaimingEvolutionFees.value = false
  }
}

const reconcilingEvolutionFees = ref(false)
const companionIntegrityScanProgress = ref('')
const companionContinuityRunStorageKey = tavernStorageKey('advs1:last-companion-continuity-repair-run')
const lastCompanionContinuityRunId = ref(
  window.localStorage.getItem(companionContinuityRunStorageKey) || '',
)
const restoringCompanionContinuity = ref(false)

async function restoreCompanionContinuityRepair() {
  if (restoringCompanionContinuity.value || !lastCompanionContinuityRunId.value) return
  if (!confirm('将移除上次审核建立的伙伴谱系绑定；只有当前实例仍精确等于执行后状态时才回退同期阶级补正。物品名称与描述从未由该功能改写。确定继续吗？')) return
  restoringCompanionContinuity.value = true
  try {
    const res = await store.adminRestoreCompanionContinuityRepair(
      lastCompanionContinuityRunId.value,
    )
    if (res.code !== 200) {
      message.error(res.message || '伙伴谱系绑定移除失败')
      return
    }
    window.localStorage.removeItem(companionContinuityRunStorageKey)
    lastCompanionContinuityRunId.value = ''
    message.success(
      `已移除 ${res.data?.restored_count ?? 0} 条伙伴谱系/复核记录；`
      + `回退阶级 ${res.data?.stage_restored_count ?? 0} 条，`
      + `因当前状态变化而仅保留现状 ${res.data?.stage_skipped_count ?? 0} 条`,
    )
  } catch (e: any) {
    message.error(e?.response?.data?.message || '伙伴谱系绑定移除失败')
  } finally {
    restoringCompanionContinuity.value = false
  }
}

async function reconcileCompanionEvolutionFees() {
  if (reconcilingEvolutionFees.value) return
  reconcilingEvolutionFees.value = true
  try {
    await runCompanionEvolutionReconciliation()
  } finally {
    companionIntegrityScanProgress.value = ''
    reconcilingEvolutionFees.value = false
  }
}

function companionIntegritySourceLabel(source: string) {
  return ({
    evolution: '嬗变记录',
    listing: '市场成交',
    barter: '以物易物',
    buy_offer: '求购成交',
    continuity: '伙伴谱系',
    continuity_dirty: '变更后的伙伴谱系',
  } as Record<string, string>)[source] || '历史记录'
}

async function loadCompanionEvolutionIntegrityPreview() {
  let previousProgressKey = ''
  while (true) {
    const res = await store.adminPreviewCompanionEvolutionIntegrity()
    if (res.code !== 200) {
      throw new Error(res.message || '费用、阶级与伙伴谱系预览失败')
    }
    const scan = res.data?.scan ?? {}
    if (scan.complete) return res.data
    const progressKey = String(scan.progress_key || '')
    if (progressKey && progressKey === previousProgressKey) {
      throw new Error('扫描水位没有推进，请稍后重试')
    }
    previousProgressKey = progressKey
    const mode = scan.mode === 'full_baseline' ? '首次建立基线' : '增量续扫'
    companionIntegrityScanProgress.value = (
      `${mode}：${companionIntegritySourceLabel(String(scan.source || ''))}`
      + `（已缓存 ${Number(scan.cached_events || 0)} 条）…`
    )
  }
}

async function runCompanionEvolutionReconciliation() {
  let preview: any
  let stagePreview: any
  try {
    const integrityPreview = await loadCompanionEvolutionIntegrityPreview()
    preview = integrityPreview?.fee
    stagePreview = integrityPreview?.stage
  } catch (e: any) {
    message.error(e?.response?.data?.message || e?.message || '预览失败'); return
  }
  const items: any[] = preview?.items ?? []
  const summary = preview?.summary ?? {}
  let stageCandidates: any[] = stagePreview?.candidates ?? []
  const continuityAudit = stagePreview?.expedition_continuity_audit ?? {}
  const continuityRisks: any[] = continuityAudit?.risk_samples ?? []
  const continuityRiskCount = Number(
    continuityAudit?.risk_count ?? continuityRisks.length,
  )
  const continuityScanTruncated = Boolean(continuityAudit?.scan_truncated)
  const continuityScanNote = continuityScanTruncated
    ? `探险伙伴连续性仅扫描最近 ${continuityAudit?.scan_limit ?? continuityAudit?.scanned_logs ?? 0} 条已完成日志，未覆盖全部历史。`
    : `探险伙伴连续性已扫描 ${continuityAudit?.scanned_logs ?? 0} 条已完成日志。`
  let continuityRepairPreview: any = null
  let continuityAiError = ''
  if (continuityRisks.length > 0) {
    const selectedLogIds = Array.from(new Set(
      continuityRisks.slice(0, 12).map((item) => Number(item.expedition_log_id)),
    )).filter((item) => Number.isInteger(item) && item > 0)
    try {
      const evidenceRes = await store.adminCompanionContinuityReviewEvidence(selectedLogIds)
      if (evidenceRes.code !== 200) {
        continuityAiError = evidenceRes.message || '读取伙伴连续性证据失败'
      } else {
        const aiRes = await store.adminCompanionContinuityAiReviewPreview(
          String(evidenceRes.data?.evidence_token || ''),
        )
        if (aiRes.code !== 200) {
          continuityAiError = aiRes.message || '伙伴连续性 AI 复核失败'
        } else {
          continuityRepairPreview = aiRes.data?.repair_preview ?? null
        }
      }
    } catch (e: any) {
      continuityAiError = e?.response?.data?.message || '伙伴连续性 AI 复核失败'
    }
  }
  const continuityExecutableItems: any[] = continuityRepairPreview?.items ?? []
  const continuityBindingItems = continuityExecutableItems.filter(
    (item) => item.mode === 'bind_only' || item.mode === 'stage_repair',
  )
  const continuityStageRepairItems = continuityExecutableItems.filter(
    (item) => item.mode === 'stage_repair',
  )
  const continuityReviewOnlyItems = continuityExecutableItems.filter(
    (item) => item.mode === 'review_only',
  )
  const continuityReviewedNoopCount = continuityReviewOnlyItems.length
  const continuityAiUnresolvedCount = Number(continuityRepairPreview?.unresolved_count || 0)
  const continuityReviewedCaseCount = continuityExecutableItems.length + continuityAiUnresolvedCount
  const continuityUnreviewedOverflow = Math.max(0, continuityRiskCount - continuityReviewedCaseCount)
  const continuityUnresolvedCount = continuityAiUnresolvedCount + continuityUnreviewedOverflow
  const stageAuditOnlyCount = items.filter((item) => item.stage_audit_required).length
  if (
    items.length === 0
    && stageCandidates.length === 0
    && continuityRiskCount === 0
  ) {
    message.info(
      `伙伴当前阶级正确，可核实的嬗变费回收账目也已平衡；`
      + `费用歧义链跳过 ${summary.ambiguous_skipped ?? 0} 项，`
      + `旧回收待人工核验 ${summary.unresolved_historical_reclaim_count ?? 0} 项，`
      + `阶级歧义组跳过 ${stagePreview?.ambiguous ?? 0} 项。`
      + continuityScanNote,
    )
    return
  }
  const feeSample = items.slice(0, 12).map((item) => {
    const adjustment = Number(item.adjustment_to_apply || 0)
    const action = adjustment > 0
      ? `补扣 ${formatGold(item.gold_to_deduct)} 金币`
      : adjustment < 0
        ? `退回 ${formatGold(item.gold_to_refund)} 金币`
        : '费用已平衡，仅核准该次正确阶级'
    return (
      `${item.adventurer_name || `#${item.adventurer_id}`}·${item.item_name}：`
      + `当次记录 ${item.before?.actual_stage ?? 0}阶，应按 ${item.after?.expected_stage ?? 0}阶计费；`
      + `正确差价 ${formatGold(item.correct_gold_due)}，已净扣 ${formatGold(item.already_collected)}，${action}`
    )
  }).join('\n')
  const feeOmitted = items.length > 12 ? `\n……另有 ${items.length - 12} 项费用账目` : ''
  const stageSample = stageCandidates.slice(0, 12).map((item) => (
    `${item.adventurer_name || `#${item.adventurer_id}`}·${item.item_name}：`
    + `伙伴当前 ${item.current_stage}阶→恢复为 ${item.target_stage}阶`
  )).join('\n')
  const stageOmitted = stageCandidates.length > 12
    ? `\n……另有 ${stageCandidates.length - 12} 个伙伴需要恢复阶级`
    : ''
  const feeSection = items.length > 0
    ? `\n\n【费用校准】\n${feeSample}${feeOmitted}`
    : '\n\n【费用校准】账目已平衡，无需扣补。'
  const stageSection = stageCandidates.length > 0
    ? `\n\n【当前伙伴阶级修复】\n${stageSample}${stageOmitted}`
    : stageAuditOnlyCount > 0
      ? '\n\n【当前伙伴阶级修复】先核准上述阶级证据，随后会立即重新扫描并修复当前伙伴。'
      : '\n\n【当前伙伴阶级修复】当前阶级正确，无需修改。'
  const continuityRiskSample = continuityRisks.slice(0, 12)
    .map(formatCompanionContinuityRisk)
    .join('\n')
  const continuityRiskOmitted = continuityRiskCount > 12
    ? `\n……另有 ${continuityRiskCount - 12} 项风险未在弹窗展开`
    : ''
  const continuityBindingSample = continuityBindingItems.slice(0, 12).map((item) => {
    const evidenceText = (
      item.review_receipt?.lineage_context?.evidence_records ?? []
    ).map((record: any) => String(record?.text || '').trim())
      .filter(Boolean)
      .join('；')
      .slice(0, 240)
    return (
      `探险#${item.expedition_log_id}·${item.adventurer_name || `#${item.adventurer_id}`}：`
      + `确认旧伙伴「${item.consumed_name || '未命名伙伴'}」与当前`
      + `「${item.target?.name || '未命名伙伴'}」为同一实体；`
      + (
        item.mode === 'stage_repair'
          ? `阶级 ${item.target?.current_stage ?? 0}→${item.target_stage ?? 0}`
          : '仅建立谱系，不改当前数据'
      )
      + `；AI 依据：${item.reason || '未提供'}`
      + (evidenceText ? `；冻结剧情：${evidenceText}` : '')
    )
  }).join('\n')
  const continuityReviewOnlySample = continuityReviewOnlyItems.slice(0, 12).map((item) => (
    `探险#${item.expedition_log_id}·${item.adventurer_name || `#${item.adventurer_id}`}：`
    + `不建立谱系；AI 依据：${item.reason || '未提供'}`
  )).join('\n')
  const continuitySection = continuityRiskCount > 0
    ? continuityAiError
      ? (
        `\n\n【探险伙伴连续性】\n${continuityRiskSample}${continuityRiskOmitted}`
        + `\n${continuityScanNote}`
        + `\n自动复核未生成可执行预览：${continuityAiError}；本次不会修改这些记录。`
      )
      : (
        `\n\n【探险伙伴连续性自动复核】\n`
        + (continuityBindingSample || '没有足以建立同一实体谱系的记录。')
        + (continuityReviewOnlySample
          ? `\n【不绑定的审核结论】\n${continuityReviewOnlySample}`
          : '')
        + `\n已生成 ${continuityBindingItems.length} 项谱系绑定，`
        + `其中 ${continuityStageRepairItems.length} 项有精确阶级补正；`
        + `${continuityReviewedNoopCount} 项仅登记审核结论，`
        + `${continuityUnresolvedCount} 项证据不足或未纳入本批。`
        + '\n该流程不会改写伙伴名称或描述。'
        + `\n${continuityScanNote}`
      )
    : `\n\n【探险伙伴连续性】未发现结构化风险。${continuityScanNote}`
  if (
    items.length === 0
    && stageCandidates.length === 0
    && continuityExecutableItems.length === 0
  ) {
    alert(
      `校验发现 ${continuityRiskCount} 项探险伙伴连续性风险，但没有可安全建立的谱系绑定。`
      + continuitySection,
    )
    return
  }
  if (!confirm(
    `发现 ${summary.candidate_count ?? items.length} 项嬗变费账目需要校准，`
    + `涉及 ${summary.adventurer_count ?? 0} 名冒险者；`
    + `合计退回 ${formatGold(summary.gold_to_refund)} 金币、补扣 ${formatGold(summary.gold_to_deduct)} 金币；`
    + `当前已发现 ${stageCandidates.length} 个伙伴需要恢复，`
    + `另有 ${stageAuditOnlyCount} 项需先核准阶级证据后重新扫描；`
    + `伙伴连续性谱系绑定 ${continuityBindingItems.length} 项。`
    + feeSection
    + stageSection
    + continuitySection
    + `\n\n${summary.calculation_basis || ''}`
    + `\n费用歧义链跳过 ${summary.ambiguous_skipped ?? 0} 项，`
    + `旧回收待人工核验 ${summary.unresolved_historical_reclaim_count ?? 0} 项，`
    + `阶级歧义组跳过 ${stagePreview?.ambiguous ?? 0} 项。`
    + '\n不会主动修改伙伴名称或描述；谱系供未来剧情读取，阶级补正只升不降并自动备份。确定执行吗？',
  )) return
  try {
    let goldRefunded = 0
    let goldDeducted = 0
    if (items.length > 0) {
      const feeRes = await store.adminExecuteCompanionEvolutionFeeReconciliation(
        String(preview.preview_token || ''),
        '伙伴嬗变费历史回收双向差额校准',
      )
      if (feeRes.code !== 200) {
        message.error(feeRes.message || '费用校准失败')
        return
      }
      goldRefunded = Number(feeRes.data?.summary?.gold_refunded || 0)
      goldDeducted = Number(feeRes.data?.summary?.gold_deducted || 0)
    }
    let boundContinuity = 0
    let stageRepairedContinuity = 0
    let reviewedContinuity = 0
    if (continuityExecutableItems.length > 0) {
      const continuityRes = await store.adminExecuteCompanionContinuityRepair(
        String(continuityRepairPreview?.preview_token || ''),
      )
      if (continuityRes.code !== 200) {
        message.error(
          `${items.length > 0 ? '费用已完成校准，但' : ''}`
          + `伙伴谱系绑定失败：${continuityRes.message || '请重新点击按钮重试'}`,
        )
        return
      }
      boundContinuity = Number(continuityRes.data?.bound_count || 0)
      stageRepairedContinuity = Number(continuityRes.data?.stage_repaired_count || 0)
      reviewedContinuity = Number(continuityRes.data?.reviewed_count || 0)
      const runId = String(continuityRes.data?.run_id || '')
      if (runId) {
        lastCompanionContinuityRunId.value = runId
        window.localStorage.setItem(companionContinuityRunStorageKey, runId)
      }
    }
    if (items.length > 0 || continuityExecutableItems.length > 0) {
      let refreshedIntegrity: any
      try {
        refreshedIntegrity = await loadCompanionEvolutionIntegrityPreview()
      } catch (e: any) {
        message.error(
          `${items.length > 0 || boundContinuity > 0 ? '费用或谱系绑定已完成，但' : ''}`
          + '重新核对伙伴阶级失败：'
          + `${e?.response?.data?.message || e?.message || '请重新点击按钮重试阶级修复'}`,
        )
        return
      }
      stageCandidates = refreshedIntegrity?.stage?.candidates ?? []
    }
    let repairedStages = 0
    if (stageCandidates.length > 0) {
      const stageRes = await store.adminRepairCompanionEvolutionStages()
      if (stageRes.code !== 200) {
        message.error(
          `${items.length > 0 ? '费用已完成校准，但' : ''}伙伴阶级修复失败：`
          + `${stageRes.message || '请重新点击按钮重试阶级修复'}`,
        )
        return
      }
      repairedStages = Number(stageRes.data?.repaired || 0)
    }
    message.success(
      `校准完成：恢复 ${repairedStages} 个伙伴的当前阶级，`
      + `伙伴谱系绑定 ${boundContinuity} 个（同期阶级补正 ${stageRepairedContinuity} 个）、`
      + `登记复核 ${reviewedContinuity} 项，`
      + `退回 ${formatGold(goldRefunded)} 金币，补扣 ${formatGold(goldDeducted)} 金币`
      + (
        continuityUnresolvedCount > 0
          ? `；另有 ${continuityUnresolvedCount} 项因证据不足未修改`
          : ''
      ),
    )
  } catch (e: any) {
    message.error(e?.response?.data?.message || '校准失败')
  }
}

const loading = ref(false)
const submitting = ref(false)
const deletingId = ref<number | null>(null)
const showModal = ref(false)
const editingItem = ref<CatalogItem | null>(null)
const showHistoryModal = ref(false)
const historyItem = ref<CatalogItem | null>(null)
const historyPreview = ref<CatalogHistorySyncPreview | null>(null)
const historyReceipt = ref('')
const historyPreviewing = ref(false)
const historyExecuting = ref(false)
const historyRestoring = ref(false)
const historySessionId = ref(0)
const historyFormRevision = ref(0)

const search = ref('')
const typeFilter = ref('')
const rarityFilter = ref('')

interface CatalogItem {
  id: number
  name: string
  type: string
  description: string
  rarity: string
  world_unique: boolean
  is_starter: boolean
  text_locked?: boolean
  is_hidden_boss_clue?: boolean
  default_uses_left?: number | null
  use_condition?: Record<string, unknown> | null
}

const items = ref<CatalogItem[]>([])
const dungeonList = ref<string[]>([])

const historyForm = reactive({
  name: '',
  type: '',
  description: '',
  rarity: '',
  is_immutable: false,
  has_default_uses_left: false,
  default_uses_left: 0,
  note: '',
})

const historyDisplayedCatalog = computed(
  () => historyPreview.value?.catalog_item || historyItem.value,
)

function catalogItemIsImmutable(item: CatalogItem | CatalogHistorySyncPreview['catalog_item'] | null) {
  if (!item) return false
  return ('is_immutable' in item && item.is_immutable === true)
    || ('text_locked' in item && item.text_locked === true)
    || (
      'use_condition' in item
      && (
        item.use_condition?.immutable === true
        || item.use_condition?.text_locked === true
      )
    )
}

const historyDisplayedImmutable = computed(
  () => catalogItemIsImmutable(historyDisplayedCatalog.value),
)

const historyCatalogChangedSinceOpen = computed(() => {
  const opened = historyItem.value
  const current = historyPreview.value?.catalog_item
  if (!opened || !current) return false
  return (
    opened.name !== current.name
    || opened.type !== current.type
    || (opened.description || '') !== (current.description || '')
    || opened.rarity !== current.rarity
    || catalogItemIsImmutable(opened) !== current.is_immutable
    || itemDefaultUsesLeft(opened) !== (current.default_uses_left ?? null)
  )
})

const historyFormFingerprint = computed(() => JSON.stringify([
  historyForm.name,
  historyForm.type,
  historyForm.description,
  historyForm.rarity,
  historyForm.is_immutable,
  historyForm.has_default_uses_left,
  historyForm.default_uses_left,
]))

const historyBusy = computed(
  () => (
    historyPreviewing.value
    || historyExecuting.value
    || historyRestoring.value
  ),
)

const historyTargetLabels = {
  equipment: '玩家随身',
  warehouse: '玩家仓库',
  market_listings: '在售挂单',
  market_offers: '待处理换物报价',
  market_buy_offers: '待处理求购展示',
  secret_auction_lots: '开放隐秘拍卖',
} as const

function historyTargetLabel(target: CatalogHistorySyncSample['target']) {
  return historyTargetLabels[target] || target
}

const historyCountRows = computed(() => {
  const counts = historyPreview.value?.counts
  if (!counts) return []
  return Object.entries(historyTargetLabels).map(([key, label]) => ({
    key,
    label,
    count: counts[key as keyof typeof historyTargetLabels] || 0,
  }))
})

const canExecuteHistorySync = computed(
  () => (
    !historyBusy.value
    && !!historyPreview.value?.preview_token
    && (historyPreview.value?.counts.total || 0) > 0
    && !historyPreview.value?.blocked_reason
  ),
)

const form = reactive<CatalogUseConditionFormSlice & {
  name: string
  description: string
  rarity: string
  type: string
  world_unique: boolean
  is_starter: boolean
}>({
  name: '',
  description: '',
  rarity: 'common',
  type: S1_DEFAULT_ITEM_TYPE,
  world_unique: false,
  is_starter: false,
  uses_left: null,
  uc_enabled: false,
  uc_dungeon: '',
  uc_target_class: '',
  text_locked: false,
})

const isEditingLocked = computed(
  () => !!editingItem.value?.is_hidden_boss_clue,
)

function itemDefaultUsesLeft(item: CatalogItem): number | null {
  return extractDefaultUsesLeft(item.use_condition, item.default_uses_left ?? null)
}

let debounceTimer: ReturnType<typeof setTimeout> | null = null
let fetchListRequestId = 0
function debouncedFetch() {
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => fetchList(), 300)
}

async function fetchList() {
  const requestId = ++fetchListRequestId
  loading.value = true
  try {
    const res = await store.adminSearchItemCatalog({
      search: search.value || undefined,
      type: typeFilter.value || undefined,
      rarity: rarityFilter.value || undefined,
    })
    if (requestId !== fetchListRequestId) return
    if (res.code === 200 && Array.isArray(res.data)) {
      items.value = res.data
    } else {
      message.error(res.message || '加载失败')
    }
  } finally {
    if (requestId === fetchListRequestId) loading.value = false
  }
}

function openCreate() {
  editingItem.value = null
  form.name = ''
  form.description = ''
  form.rarity = 'common'
  form.type = S1_DEFAULT_ITEM_TYPE
  form.world_unique = false
  form.is_starter = false
  form.uc_enabled = false
  form.uc_dungeon = ''
  form.uc_target_class = ''
  form.text_locked = false
  form.uses_left = null
  showModal.value = true
}

function openEdit(item: CatalogItem) {
  editingItem.value = item
  form.name = item.name
  form.description = item.description
  form.rarity = item.rarity
  form.type = item.type
  form.world_unique = item.world_unique
  form.is_starter = item.is_starter
  parseCatalogUseConditionToForm(item.use_condition, form)
  form.uses_left = extractDefaultUsesLeft(item.use_condition, item.default_uses_left ?? null)
  showModal.value = true
}

function closeModal() {
  showModal.value = false
  editingItem.value = null
}

function openHistorySync(item: CatalogItem) {
  historySessionId.value += 1
  historyItem.value = item
  historyForm.name = item.name
  historyForm.type = item.type
  historyForm.description = item.description || ''
  historyForm.rarity = item.rarity
  historyForm.is_immutable = catalogItemIsImmutable(item)
  historyForm.has_default_uses_left = false
  historyForm.default_uses_left = itemDefaultUsesLeft(item) ?? 0
  historyForm.note = ''
  historyPreview.value = null
  historyReceipt.value = ''
  showHistoryModal.value = true
}

function closeHistorySync() {
  if (historyBusy.value) return
  historySessionId.value += 1
  showHistoryModal.value = false
  historyItem.value = null
  historyPreview.value = null
  historyReceipt.value = ''
}

function buildHistoryOldSnapshot(): CatalogHistorySyncOldSnapshotInput | null {
  const name = historyForm.name.trim()
  const type = historyForm.type.trim()
  const rarity = historyForm.rarity.trim()
  if (!name || !type || !rarity) {
    message.warning('请完整填写修改前的名称、类型和稀有度')
    return null
  }
  const snapshot: CatalogHistorySyncOldSnapshotInput = {
    name,
    type,
    description: historyForm.description.trim(),
    rarity,
    is_immutable: historyForm.is_immutable,
  }
  if (historyForm.has_default_uses_left) {
    const defaultUses = Number(historyForm.default_uses_left)
    if (!Number.isInteger(defaultUses) || defaultUses < 0) {
      message.warning('修改前默认使用次数必须是非负整数')
      return null
    }
    snapshot.default_uses_left = defaultUses
  }
  return snapshot
}

async function previewHistorySync(options: { quiet?: boolean } = {}) {
  if (historyPreviewing.value || !historyItem.value) return
  const snapshot = buildHistoryOldSnapshot()
  if (!snapshot) return
  const requestItemId = historyItem.value.id
  const requestSessionId = historySessionId.value
  const requestRevision = historyFormRevision.value
  const requestFingerprint = historyFormFingerprint.value
  historyPreviewing.value = true
  try {
    const res = await store.adminPreviewCatalogHistorySync(
      requestItemId,
      snapshot,
    )
    if (
      !showHistoryModal.value
      || historySessionId.value !== requestSessionId
      || historyFormRevision.value !== requestRevision
      || historyItem.value?.id !== requestItemId
      || historyFormFingerprint.value !== requestFingerprint
    ) {
      return
    }
    if (res.code !== 200) {
      message.error(res.message || '历史同步预览失败')
      historyPreview.value = null
      return
    }
    historyPreview.value = res.data
    if (!options.quiet) {
      if (res.data?.blocked_reason) {
        message.warning(res.data.blocked_reason)
      } else if ((res.data?.counts?.total || 0) === 0) {
        message.info('没有找到需要修复的当前资产')
      } else {
        message.success(`预览完成：命中 ${res.data.counts.total} 条当前资产`)
      }
    }
  } catch (e: any) {
    historyPreview.value = null
    message.error(e?.response?.data?.message || '历史同步预览失败')
  } finally {
    historyPreviewing.value = false
  }
}

async function executeHistorySync() {
  const item = historyItem.value
  const preview = historyPreview.value
  if (!item || !canExecuteHistorySync.value || !preview) return
  const old = preview.old_snapshot
  const current = preview.catalog_item
  if (!confirm(
    `确认按本次预览修复 ${preview.counts.total} 条当前资产吗？`
    + `\n修改前：${old.name} / ${old.type} / ${old.rarity}`
    + `\n当前：${current.name} / ${current.type} / ${current.rarity}`
    + '\n系统会再次核对图鉴和全部候选；任一变化都会整批拒绝。',
  )) return
  historyExecuting.value = true
  try {
    const res = await store.adminExecuteCatalogHistorySync(
      item.id,
      preview.preview_token,
      historyForm.note.trim(),
    )
    if (res.code !== 200) {
      message.error(res.message || '历史同步修复失败')
      historyPreview.value = null
      return
    }
    const repaired = Number(res.data?.repaired?.total || 0)
    const runId = res.data?.run_id || '无批次'
    historyReceipt.value = `执行完成：修复 ${repaired} 条，批次 ${runId}。`
    message.success(res.message || historyReceipt.value)
    try {
      await fetchList()
      await previewHistorySync({ quiet: true })
    } catch {
      message.warning('历史修复已经完成，但图鉴列表刷新失败；请稍后重新打开面板')
    }
  } catch (e: any) {
    historyPreview.value = null
    message.error(e?.response?.data?.message || '历史同步修复失败，请重新预览')
  } finally {
    historyExecuting.value = false
  }
}

async function restoreHistorySync() {
  const item = historyItem.value
  const runId = historyPreview.value?.latest_backup_run_id
  const summary = historyPreview.value?.latest_backup_summary
  if (!item || !runId || historyRestoring.value) return
  const transition = (
    summary?.old_snapshot && summary?.current_snapshot
      ? `\n批次内容：${summary.old_snapshot.name} → ${summary.current_snapshot.name}`
      : ''
  )
  if (!confirm(
    `确认尝试还原最近历史同步批次吗？`
    + `${transition}`
    + `\n影响记录：${summary?.repaired.total ?? '未知'} 条`
    + `\n批次：${runId}`
    + '\n修复后已被玩家移动、交易或再次修改的记录会保留现状。',
  )) return
  historyRestoring.value = true
  try {
    const res = await store.adminRestoreCatalogHistorySync(item.id, runId)
    if (res.code !== 200) {
      message.error(res.message || '历史同步还原失败')
      historyPreview.value = null
      return
    }
    const restored = Number(res.data?.restored?.total || 0)
    const skipped = (
      Number(res.data?.skipped_changed || 0)
      + Number(res.data?.skipped_frozen_history || 0)
      + Number(res.data?.skipped_missing || 0)
    )
    historyReceipt.value = `还原完成：恢复 ${restored} 条，安全跳过 ${skipped} 条。`
    message.success(res.message || historyReceipt.value)
    try {
      await fetchList()
      await previewHistorySync({ quiet: true })
    } catch {
      message.warning('历史还原已经完成，但图鉴列表刷新失败；请稍后重新打开面板')
    }
  } catch (e: any) {
    historyPreview.value = null
    message.error(e?.response?.data?.message || '历史同步还原失败，请重新预览')
  } finally {
    historyRestoring.value = false
  }
}

watch(
  () => [
    historyForm.name,
    historyForm.type,
    historyForm.description,
    historyForm.rarity,
    historyForm.is_immutable,
    historyForm.has_default_uses_left,
    historyForm.default_uses_left,
  ],
  () => {
    historyFormRevision.value += 1
    historyPreview.value = null
  },
)

function formatHistoryTimestamp(value: string) {
  if (!value) return '时间未知'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString()
}

async function doSubmit() {
  if (!form.name.trim()) return
  submitting.value = true
  try {
    if (editingItem.value) {
      const payload: Record<string, unknown> = {}
      if (!isEditingLocked.value) {
        payload.type = form.type
        payload.rarity = form.rarity
        payload.world_unique = form.world_unique
        payload.is_starter = form.is_starter
        payload.name = form.name.trim()
        payload.description = form.description.trim()
        payload.use_condition = buildCatalogUseCondition(form)
        appendUsesLeftPayload(payload, form.uses_left)
      }
      const res = await store.adminUpdateCatalogItem(editingItem.value.id, payload)
      if (res.code === 200) {
        message.success(res.message || '已更新')
        closeModal()
        await fetchList()
      } else {
        message.error(res.message || '更新失败')
      }
    } else {
      // 新增
      const createPayload: Record<string, unknown> = {
        name: form.name.trim(),
        type: form.type,
        description: form.description.trim(),
        rarity: form.rarity,
        world_unique: form.world_unique,
        is_starter: form.is_starter,
        use_condition: buildCatalogUseCondition(form),
      }
      appendUsesLeftPayload(createPayload, form.uses_left)
      const res = await store.adminCreateCatalogItem(createPayload as Parameters<typeof store.adminCreateCatalogItem>[0])
      if (res.code === 200 || res.code === 201) {
        message.success(res.message || '已添加')
        closeModal()
        await fetchList()
      } else {
        message.error(res.message || '添加失败')
      }
    }
  } finally {
    submitting.value = false
  }
}

async function confirmDelete(item: CatalogItem) {
  if (!confirm(`确定要删除图鉴物品「${item.name}」吗？\n注意：若有冒险者持有该物品则无法删除。`)) return
  deletingId.value = item.id
  try {
    const res = await store.adminDeleteCatalogItem(item.id)
    if (res.code === 200) {
      message.success(res.message || '已删除')
      await fetchList()
    } else {
      message.error(res.message || '删除失败')
    }
  } finally {
    deletingId.value = null
  }
}

onMounted(() => {
  void fetchList()
  if (!isReadonly.value) {
    void store.adminGetDungeonList().then(list => { dungeonList.value = list })
  }
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
.filter-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-bottom: 1rem;
}
.filter-bar input,
.filter-bar select {
  padding: 0.35rem 0.6rem;
  border: 1px solid var(--color-border, #ddd);
  border-radius: 6px;
  background: var(--color-bg, #fff);
  color: var(--color-text, #333);
  font-size: 0.85rem;
}
.filter-bar input {
  flex: 1;
  min-width: 150px;
}
.catalog-list {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.catalog-item {
  padding: 0.5rem 0.65rem;
  border-radius: 6px;
  background: var(--color-bg, #fff);
  border: 1px solid var(--color-border, #eee);
}
.catalog-item-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem;
  margin-bottom: 0.2rem;
}
.catalog-item-name {
  font-weight: 600;
}
.catalog-item-type {
  font-size: 0.8rem;
  color: var(--color-text-secondary, #666);
}
.catalog-item-desc {
  font-size: 0.8rem;
  color: var(--color-text-secondary, #666);
  line-height: 1.4;
  margin: 0.2rem 0 0.4rem;
}
.catalog-item-actions {
  display: flex;
  gap: 0.5rem;
}
.rarity-tag {
  font-size: 0.72rem;
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
}
.r-common { background: #f3f4f6; color: #6b7280; }
.r-uncommon { background: #d1fae5; color: #065f46; }
.r-rare { background: #dbeafe; color: #1e40af; }
.r-epic { background: #ede9fe; color: #5b21b6; }
.r-legendary { background: #fef3c7; color: #92400e; }
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
.badge-starter {
  background: #dcfce7;
  color: #166534;
  border: 1px solid #86efac;
}
.badge-dungeon {
  background: #fef9c3;
  color: #854d0e;
  border: 1px solid #fde047;
}
.badge-locked {
  background: rgba(240, 136, 62, 0.12);
  color: #c2410c;
  border: 1px solid rgba(240, 136, 62, 0.35);
}
.badge-uses {
  background: rgba(99, 102, 241, 0.12);
  color: #4338ca;
  border: 1px solid rgba(99, 102, 241, 0.35);
}
.locked-hint {
  margin: 0 0 12px;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text-secondary);
  background: rgba(240, 136, 62, 0.08);
  border: 1px dashed rgba(240, 136, 62, 0.35);
}
.uc-section {
  margin-top: 0.25rem;
  padding-top: 0.5rem;
  border-top: 1px dashed var(--color-border, #ddd);
}
.uc-fields {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  margin-top: 0.4rem;
  padding-left: 0.25rem;
}
.btn-sm {
  font-size: 0.8rem;
  padding: 0.2rem 0.5rem;
}
.btn-danger {
  background: #fee2e2;
  color: #991b1b;
  border: 1px solid #fca5a5;
  cursor: pointer;
}
.btn-danger:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ── Teleport 弹窗样式 ── */
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
  width: min(520px, 90vw);
  max-height: 85vh;
  overflow-y: auto;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.2);
}
.history-sync-modal {
  width: min(820px, 94vw);
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
.form-row {
  display: flex;
  gap: 0.75rem;
}
.form-row .form-label {
  flex: 1;
}
.modal-footer {
  display: flex;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 0.5rem;
  padding: 0.75rem 1.25rem;
  border-top: 1px solid var(--color-border, #eee);
}
.history-safety,
.history-current,
.history-notice,
.history-blocked,
.history-empty,
.history-receipt,
.history-backup {
  margin: 0 0 0.8rem;
  padding: 0.7rem 0.8rem;
  border-radius: 7px;
  font-size: 0.82rem;
  line-height: 1.55;
}
.history-safety {
  color: #854d0e;
  background: #fefce8;
  border: 1px solid #fde68a;
}
.history-current {
  display: grid;
  gap: 0.2rem;
  color: var(--color-text, #333);
  background: var(--color-bg-secondary, #f8fafc);
  border: 1px solid var(--color-border, #e5e7eb);
}
.history-current p {
  margin: 0;
  color: var(--color-text-secondary, #666);
  white-space: pre-wrap;
}
.history-form {
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
}
.history-notice {
  color: #1e40af;
  background: #eff6ff;
  border: 1px solid #bfdbfe;
}
.history-blocked {
  color: #991b1b;
  background: #fef2f2;
  border: 1px solid #fecaca;
}
.history-empty {
  color: #1e3a8a;
  background: #eff6ff;
  border: 1px solid #bfdbfe;
}
.history-counts {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0.5rem;
  margin: 0.8rem 0;
}
.history-count-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  padding: 0.55rem 0.65rem;
  border: 1px solid var(--color-border, #e5e7eb);
  border-radius: 7px;
  font-size: 0.78rem;
}
.history-count-card strong {
  font-size: 1rem;
}
.history-summary {
  margin: 0.5rem 0;
  color: var(--color-text-secondary, #666);
  font-size: 0.8rem;
  line-height: 1.5;
}
.history-samples {
  margin-top: 0.7rem;
  border-top: 1px dashed var(--color-border, #ddd);
  padding-top: 0.6rem;
  font-size: 0.8rem;
}
.history-samples summary {
  cursor: pointer;
  font-weight: 600;
}
.history-sample {
  display: grid;
  gap: 0.15rem;
  margin-top: 0.45rem;
  padding: 0.5rem;
  border-radius: 6px;
  background: var(--color-bg-secondary, #f8fafc);
}
.history-sample span,
.history-sample small {
  overflow-wrap: anywhere;
  color: var(--color-text-secondary, #666);
}
.history-backup {
  display: grid;
  gap: 0.15rem;
  margin-top: 0.8rem;
  color: #5b21b6;
  background: #f5f3ff;
  border: 1px solid #ddd6fe;
  overflow-wrap: anywhere;
}
.history-backup small {
  color: #6d28d9;
}
.history-receipt {
  color: #166534;
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
}
.form-label small {
  color: var(--color-text-secondary, #666);
  font-weight: 400;
}
@media (max-width: 720px) {
  .history-counts {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .history-sync-modal .form-row {
    flex-direction: column;
    gap: 0;
  }
  .history-footer > button {
    flex: 1 1 45%;
  }
}
</style>
