<template>
  <div class="fate-repair">
    <div class="repair-intro">
      <div>
        <h6>探险破坏性变更核查与安全修复</h6>
        <p>
          扫描最终命途被降级后，新成果没有落库、旧技能或旧特质却已被删除的历史事故。
          同时一键列出缺少逐项删除回执的旧最终命途删除，以及伙伴被整件替换后阶级、类型或完整描述
          没有连续保留的历史风险；
          恢复冻结快照能够机械证明的旧数据，并确保被拦截者仍有最终试炼资格；
          不会猜测跨名伙伴归属，也不会覆盖已经继续演化的当前装备。
        </p>
      </div>
      <span class="safety-badge">预览后执行</span>
    </div>

    <div class="scope-row">
      <label>
        <span>限定探险日志（可选）</span>
        <input
          v-model.trim="expeditionLogIdInput"
          type="number"
          min="1"
          inputmode="numeric"
          placeholder="留空扫描全部事故"
          :disabled="busy"
          @input="resetHistoricalReview"
        />
      </label>
      <button
        type="button"
        class="btn-primary btn-sm"
        :disabled="busy || !expeditionLogIdInput"
        @click="loadHistoricalReviewEvidence"
      >
        {{ evidenceLoading ? '读取中…' : '1. 读取删除证据' }}
      </button>
      <button
        type="button"
        class="btn-warning btn-sm"
        :disabled="busy || !historicalEvidence"
        @click="aiReviewHistoricalEvidence"
      >
        {{ aiReviewLoading ? '判断中…' : '2. AI 判断并生成预览' }}
      </button>
      <button
        type="button"
        class="btn-secondary btn-sm"
        :disabled="!historicalEvidence"
        @click="reviewPanelOpen = !reviewPanelOpen"
      >
        {{ reviewPanelOpen ? '收起人工查看' : '3. 人工查看证据与结论' }}
      </button>
      <button
        type="button"
        class="btn-secondary btn-sm"
        :disabled="busy"
        @click="previewRepair(false)"
      >
        {{ previewLoading ? '扫描中…' : '预览数据恢复与最终试炼资格' }}
      </button>
      <button
        type="button"
        class="btn-secondary btn-sm"
        :disabled="busy"
        @click="previewRepair(true)"
      >
        {{ previewLoading ? '扫描中…' : '一键扫描历史破坏风险' }}
      </button>
    </div>
    <p class="timeout-hint">
      前端等待上限为 120 秒；扫描期间按钮会保持“扫描中”，不会使用默认短超时。
      最近一次扫描耗时：{{ previewDurationLabel }}。
    </p>

    <section
      v-if="historicalEvidence && reviewPanelOpen"
      class="historical-review-card"
    >
      <div class="historical-review-heading">
        <div>
          <strong>
            日志 #{{ historicalEvidence.evidence.expedition_log_id }} ·
            {{ historicalEvidence.evidence.party_name || '未知小队' }} ·
            {{ historicalEvidence.evidence.target_dungeon || '未知地点' }}
          </strong>
          <p>
            冻结摘要 <code>{{ historicalEvidence.evidence.evidence_digest }}</code>；
            删除项 {{ historicalEvidence.evidence.removed_assets.length }}，
            剧情证据 {{ historicalEvidence.evidence.events.length }} 条。
          </p>
        </div>
        <span :class="['resource-tag', historicalReview ? '' : 'muted-tag']">
          {{ historicalReview ? 'AI 已逐项判断' : '等待 AI 判断' }}
        </span>
      </div>

      <details open class="repair-details">
        <summary>被删特质/技能与逐项结论</summary>
        <div class="repair-list">
          <article
            v-for="item in historicalEvidence.evidence.removed_assets"
            :key="item.action_ref"
          >
            <div class="repair-item-title">
              <span class="resource-tag">{{ resourceLabel(item.resource_type) }}</span>
              <strong>{{ item.adventurer_name }} · {{ item.name }}</strong>
            </div>
            <p v-if="item.description">冻结描述：{{ item.description }}</p>
            <p v-else class="muted-text">冻结快照没有完整描述。</p>
            <p>
              当前{{ item.currently_missing ? '仍缺失' : '已经存在' }}；
              快照{{ item.snapshot_available ? '存在' : '缺失' }}；
              同场{{ item.same_log_changed ? '存在同名结构化变更' : '无同名变更' }}；
              后续结构化触碰 {{ item.later_structured_touches.length }} 次。
            </p>
            <template v-if="reviewActionFor(item.action_ref)">
              <p class="review-decision">
                AI 结论：{{ reviewDecisionText(item.action_ref) }}
              </p>
              <p>依据：{{ reviewReason(item.action_ref) }}</p>
              <p v-if="reviewEventIndexes(item.action_ref)">
                引用剧情事件：{{ reviewEventIndexes(item.action_ref) }}
              </p>
              <p v-if="reviewSuccessorNames(item.action_ref)">
                后继项：{{ reviewSuccessorNames(item.action_ref) }}
              </p>
            </template>
            <small><code>{{ item.action_ref }}</code></small>
          </article>
        </div>
      </details>

      <details class="repair-details">
        <summary>完整剧情证据（{{ historicalEvidence.evidence.events.length }} 条）</summary>
        <div class="event-list">
          <article
            v-for="event in historicalEvidence.evidence.events"
            :key="`review-event-${event.event_index}`"
          >
            <strong>事件 {{ event.event_index }} · {{ event.source }}</strong>
            <pre>{{ event.content }}</pre>
          </article>
        </div>
      </details>

      <details
        v-if="historicalEvidence.evidence.same_log_successors.length"
        class="repair-details"
      >
        <summary>
          同场后继候选（{{ historicalEvidence.evidence.same_log_successors.length }} 项）
        </summary>
        <div class="repair-list">
          <article
            v-for="item in historicalEvidence.evidence.same_log_successors"
            :key="item.successor_ref"
          >
            <strong>{{ resourceLabel(item.resource_type) }} · {{ item.name }}</strong>
            <p v-if="item.description">{{ item.description }}</p>
            <small><code>{{ item.successor_ref }}</code></small>
          </article>
        </div>
      </details>
    </section>

    <div v-if="preview" class="preview-card">
      <div class="metric-grid">
        <div>
          <span>扫描日志</span>
          <strong>{{ preview.scanned_logs }}</strong>
        </div>
        <div>
          <span>事故日志</span>
          <strong>{{ preview.incident_logs }}</strong>
        </div>
        <div class="metric-ready">
          <span>可安全恢复</span>
          <strong>{{ preview.candidate_count }}</strong>
        </div>
        <div :class="{ 'metric-warning': preview.ambiguous_count > 0 }">
          <span>保留人工核查</span>
          <strong>{{ preview.ambiguous_count }}</strong>
        </div>
        <div class="metric-ready">
          <span>需要重开最终试炼</span>
          <strong>{{ preview.retry_reopen_count }}</strong>
        </div>
        <div class="metric-ready">
          <span>最终试炼已可再战</span>
          <strong>{{ preview.retry_already_available_count }}</strong>
        </div>
        <div>
          <span>后来已经成功（人）</span>
          <strong>{{ preview.retry_later_completed_count }}</strong>
        </div>
        <div>
          <span>后来成功已排除（项）</span>
          <strong>{{ preview.later_completed_resource_count }}</strong>
        </div>
        <div :class="{ 'metric-warning': preview.retry_ambiguous_count > 0 }">
          <span>命途状态已变化</span>
          <strong>{{ preview.retry_ambiguous_count }}</strong>
        </div>
        <div :class="{ 'metric-warning': preview.unreviewed_destructive_log_count > 0 }">
          <span>未审核删除日志</span>
          <strong>{{ preview.unreviewed_destructive_log_count }}</strong>
        </div>
        <div :class="{ 'metric-warning': preview.unreviewed_current_missing_count > 0 }">
          <span>其中当前仍缺失</span>
          <strong>{{ preview.unreviewed_current_missing_count }}</strong>
        </div>
        <div>
          <span>缺少冻结快照</span>
          <strong>{{ preview.unreviewed_snapshot_missing_count }}</strong>
        </div>
        <div :class="{ 'metric-warning': preview.companion_continuity_audit.risk_count > 0 }">
          <span>伙伴替换风险</span>
          <strong>{{ preview.companion_continuity_audit.risk_count }}</strong>
        </div>
        <div :class="{ 'metric-warning': preview.companion_continuity_audit.stage_risk_count > 0 }">
          <span>伙伴阶级回退</span>
          <strong>{{ preview.companion_continuity_audit.stage_risk_count }}</strong>
        </div>
        <div :class="{ 'metric-warning': preview.companion_continuity_audit.description_risk_count > 0 }">
          <span>伙伴描述损失</span>
          <strong>{{ preview.companion_continuity_audit.description_risk_count }}</strong>
        </div>
      </div>

      <p v-if="preview.truncated" class="warning-box">
        本次扫描没有覆盖完整范围，后端已禁止执行。请先处理扫描上限问题。
      </p>
      <p v-if="preview.companion_continuity_audit.scan_truncated" class="warning-box">
        伙伴替换核查只覆盖最近 {{ preview.companion_continuity_audit.scan_limit }} 条已完成日志；
        当前清单不是完整历史。该清单本身始终只读，不影响已确认命途修复项的执行边界。
      </p>
      <p
        v-else-if="repairActionCount === 0 && (
          preview.unreviewed_destructive_log_count > 0
          || preview.companion_continuity_audit.risk_count > 0
        )"
        class="warning-box"
      >
        发现 {{ preview.unreviewed_destructive_log_count }} 条旧最终命途删除日志、
        {{ preview.companion_continuity_audit.risk_count }} 项伙伴替换连续性风险。
        没有精确确认或已经发生后续变化的记录只进入核查清单，不会被“执行修复”按钮自动覆盖。
      </p>
      <p v-else-if="repairActionCount === 0" class="empty-box">
        当前没有需要写入的修复；数据库不会发生变化。
      </p>
      <p v-if="preview.retry_already_available_count > 0" class="ready-box">
        {{ preview.retry_already_available_count }} 名被拦截者当前已经是“最终试炼”状态，
        可以直接再次进入候选地点挑战，不需要重置整条命途。
      </p>
      <p v-if="preview.retry_later_completed_count > 0" class="empty-box">
        {{ preview.retry_later_completed_count }} 名被拦截者后来已经成功突破；
        其 {{ preview.later_completed_resource_count }} 项旧能力候选已从执行范围排除，
        不会重新开放、恢复或覆盖其现有成果。
      </p>
      <p v-if="preview.ambiguous_count > 0" class="warning-box">
        {{ preview.ambiguous_count }} 项因缺少冻结快照或后来又发生结构化变更被排除，按钮不会碰这些数据。
      </p>
      <p v-if="preview.retry_ambiguous_count > 0" class="warning-box">
        {{ preview.retry_ambiguous_count }} 名角色在事故后重置或改变了命途状态，自动修复不会覆盖其后续选择。
      </p>

      <details v-if="preview.items.length" class="repair-details">
        <summary>查看 {{ preview.items.length }} 项待恢复明细</summary>
        <div class="repair-list">
          <article
            v-for="item in preview.items"
            :key="`${item.resource_type}-${item.adventurer_id}-${item.name}`"
          >
            <div class="repair-item-title">
              <span class="resource-tag">{{ resourceLabel(item.resource_type) }}</span>
              <strong>{{ item.adventurer_name }} · {{ item.name }}</strong>
            </div>
            <p>{{ item.description || '冻结快照中没有描述，将按空描述恢复。' }}</p>
            <small>事故日志 #{{ item.incident_log_id }}</small>
          </article>
        </div>
      </details>

      <details v-if="preview.ambiguous_items.length" class="repair-details ambiguous-details">
        <summary>查看 {{ preview.ambiguous_items.length }} 项未自动处理明细</summary>
        <div class="repair-list">
          <article
            v-for="item in preview.ambiguous_items"
            :key="`${item.expedition_log_id}-${item.adventurer_id}-${item.resource_type}-${item.name}`"
          >
            <div class="repair-item-title">
              <span class="resource-tag muted-tag">{{ resourceLabel(item.resource_type) }}</span>
              <strong>冒险者 #{{ item.adventurer_id }} · {{ item.name }}</strong>
            </div>
            <p>{{ ambiguousReason(item) }}</p>
            <small>事故日志 #{{ item.expedition_log_id }}</small>
          </article>
        </div>
      </details>

      <details
        v-if="preview.unreviewed_destructive_logs.length"
        class="repair-details ambiguous-details"
      >
        <summary>
          查看 {{ preview.unreviewed_destructive_member_count }} 条未审核删除核查记录
          （共 {{ preview.unreviewed_destructive_item_count }} 项）
        </summary>
        <div class="repair-list">
          <article
            v-for="item in preview.unreviewed_destructive_logs"
            :key="`unreviewed-${item.expedition_log_id}-${item.adventurer_id}`"
          >
            <div class="repair-item-title">
              <span class="resource-tag muted-tag">只读核查</span>
              <strong>{{ item.adventurer_name }}</strong>
            </div>
            <p v-if="item.removed_traits.length">
              删除特质：{{ item.removed_traits.join('、') }}
            </p>
            <p v-if="item.removed_skills.length">
              删除技能：{{ item.removed_skills.join('、') }}
            </p>
            <p v-if="item.currently_missing_traits.length || item.currently_missing_skills.length">
              当前仍缺失：{{ [
                ...item.currently_missing_traits,
                ...item.currently_missing_skills,
              ].join('、') }}
            </p>
            <p v-if="item.snapshot_missing_traits.length || item.snapshot_missing_skills.length">
              冻结快照缺失：{{ [
                ...item.snapshot_missing_traits,
                ...item.snapshot_missing_skills,
              ].join('、') }}
            </p>
            <p v-if="item.same_log_changed_traits.length || item.same_log_changed_skills.length">
              同场另有结构化新增/替换：{{ [
                ...item.same_log_changed_traits,
                ...item.same_log_changed_skills,
              ].join('、') }}
            </p>
            <small>最终命途日志 #{{ item.expedition_log_id }}</small>
          </article>
        </div>
      </details>

      <details
        v-if="preview.companion_continuity_audit.risk_samples.length"
        class="repair-details ambiguous-details"
      >
        <summary>
          查看 {{ preview.companion_continuity_audit.risk_samples.length }} 项伙伴替换核查记录
        </summary>
        <p class="timeout-hint">{{ preview.companion_continuity_audit.calculation_basis }}</p>
        <div class="repair-list">
          <article
            v-for="item in preview.companion_continuity_audit.risk_samples"
            :key="`companion-${item.expedition_log_id}-${item.adventurer_id}-${item.consumed_name}`"
          >
            <div class="repair-item-title">
              <span class="resource-tag muted-tag">只读核查</span>
              <strong>{{ item.adventurer_name || `冒险者 #${item.adventurer_id}` }}</strong>
            </div>
            <p>
              出发伙伴：{{ item.consumed_name }}（阶级 {{ item.departure_stage }}）
            </p>
            <p v-if="item.new_items.length">
              同场新增：{{ companionNewItemsLabel(item) }}
            </p>
            <p>风险：{{ item.risk_codes.map(companionRiskLabel).join('；') }}</p>
            <small>探险日志 #{{ item.expedition_log_id }} · 不执行自动跨名修复</small>
          </article>
        </div>
      </details>

      <details
        v-if="preview.later_completed_resource_items.length"
        class="repair-details excluded-details"
      >
        <summary>
          查看 {{ preview.later_completed_resource_items.length }} 项后来成功后排除明细
        </summary>
        <div class="repair-list">
          <article
            v-for="item in preview.later_completed_resource_items"
            :key="`later-success-${item.resource_type}-${item.adventurer_id}-${item.name}`"
          >
            <div class="repair-item-title">
              <span class="resource-tag muted-tag">{{ resourceLabel(item.resource_type) }}</span>
              <strong>{{ item.adventurer_name }} · {{ item.name }}</strong>
            </div>
            <p>该角色后来已成功突破，当前缺失可能来自合法进阶、合并或替换，因此不会自动恢复。</p>
            <small>早期事故日志 #{{ item.incident_log_id }}</small>
          </article>
        </div>
      </details>

      <details v-if="preview.retry_status_items.length" class="repair-details">
        <summary>查看 {{ preview.retry_status_items.length }} 名被拦截者的最终试炼状态</summary>
        <div class="repair-list">
          <article
            v-for="item in preview.retry_status_items"
            :key="`retry-${item.adventurer_id}-${item.incident_log_id}`"
          >
            <div class="repair-item-title">
              <span :class="['resource-tag', retryStatusClass(item.retry_status)]">
                {{ retryStatusLabel(item.retry_status) }}
              </span>
              <strong>{{ item.adventurer_name }}</strong>
            </div>
            <p>{{ retryStatusDescription(item) }}</p>
            <small>被拦截日志 #{{ item.incident_log_id }}</small>
          </article>
        </div>
      </details>

      <div class="execute-row">
        <button
          type="button"
          class="btn-warning btn-sm"
          :disabled="!canExecute"
          @click="executeRepair"
        >
          {{ executeLoading ? '恢复中…' : `执行修复（${repairActionCount} 项）` }}
        </button>
        <span>执行时会复核日志和命途状态、锁定冒险者，并记录独立批次号。</span>
      </div>
    </div>

    <div v-if="executeResult" class="result-card">
      <strong>本批恢复完成</strong>
      <p>
        恢复旧技能/特质 {{ executeResult.created_count }} 项，
        重开最终试炼 {{ executeResult.reopened_final_count }} 人；
        数据跳过 {{ executeResult.skipped_count }} 项，命途跳过 {{ executeResult.retry_skipped_count }} 人。
        批次号 <code>{{ executeResult.run_id }}</code>
      </p>
    </div>

    <div class="restore-card">
      <div>
        <strong>按批次回滚</strong>
        <p>只接受明确批次号；旧能力或命途后来发生变化时，后端都会跳过，不做强制覆盖。</p>
      </div>
      <div class="restore-row">
        <input
          v-model.trim="restoreRunId"
          type="text"
          maxlength="64"
          placeholder="先执行恢复，或粘贴已有批次号"
          :disabled="busy"
        />
        <button
          type="button"
          class="btn-secondary btn-sm"
          :disabled="busy || !restoreRunId"
          @click="restoreRepair"
        >
          {{ restoreLoading ? '回滚中…' : '回滚指定批次' }}
        </button>
      </div>
      <p v-if="restoreResult" class="restore-result">
        批次 {{ restoreResult.run_id }}：已回滚 {{ restoreResult.removed_count }} 项，
        恢复原命途状态 {{ restoreResult.restored_journey_count }} 人；
        数据跳过 {{ restoreResult.skipped_count }} 项，命途跳过 {{ restoreResult.journey_skipped_count }} 人。
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import {
  useAdventurerS1Store,
  type FateHistoricalReviewAction,
  type FateHistoricalReviewEvidenceResponse,
  type FateHistoricalReviewPreviewResponse,
  type FateHistoricalReviewResult,
  type FateSettlementCompanionContinuityRiskItem,
  type FateSettlementRepairAmbiguousItem,
  type FateSettlementRepairExecuteResult,
  type FateSettlementRepairPreview,
  type FateSettlementRepairResourceType,
  type FateSettlementRepairRestoreResult,
  type FateSettlementRetryStatus,
  type FateSettlementRetryStatusItem,
} from '../stores/adventurerS1'

const store = useAdventurerS1Store()
const message = useGlobalMessage()

const expeditionLogIdInput = ref('')
const historicalEvidence = ref<FateHistoricalReviewEvidenceResponse | null>(null)
const historicalReview = ref<FateHistoricalReviewResult | null>(null)
const reviewPanelOpen = ref(false)
const preview = ref<FateSettlementRepairPreview | null>(null)
const executeResult = ref<FateSettlementRepairExecuteResult | null>(null)
const restoreResult = ref<FateSettlementRepairRestoreResult | null>(null)
const restoreRunId = ref('')
const previewDurationMs = ref<number | null>(null)
const previewLoading = ref(false)
const evidenceLoading = ref(false)
const aiReviewLoading = ref(false)
const executeLoading = ref(false)
const restoreLoading = ref(false)

const busy = computed(
  () => previewLoading.value
    || evidenceLoading.value
    || aiReviewLoading.value
    || executeLoading.value
    || restoreLoading.value,
)
const repairActionCount = computed(
  () => (preview.value?.candidate_count ?? 0)
    + (preview.value?.retry_reopen_count ?? 0),
)
const canExecute = computed(
  () => !!preview.value
    && !busy.value
    && !preview.value.truncated
    && repairActionCount.value > 0
    && !!preview.value.preview_token,
)
const previewDurationLabel = computed(() => {
  if (previewLoading.value) return '正在计时'
  if (previewDurationMs.value == null) return '尚未扫描'
  return previewDurationMs.value < 1000
    ? `${previewDurationMs.value} 毫秒`
    : `${(previewDurationMs.value / 1000).toFixed(1)} 秒`
})

function resourceLabel(resourceType: FateSettlementRepairResourceType) {
  return resourceType === 'skill' ? '技能' : '特质'
}

function resetHistoricalReview() {
  historicalEvidence.value = null
  historicalReview.value = null
  reviewPanelOpen.value = false
  preview.value = null
  executeResult.value = null
}

function reviewActionFor(actionRef: string): FateHistoricalReviewAction | undefined {
  return historicalReview.value?.actions.find((item) => item.action_ref === actionRef)
}

function reviewDecisionLabel(decision: FateHistoricalReviewAction['decision']) {
  const labels: Record<FateHistoricalReviewAction['decision'], string> = {
    preserve_existing: '保留原项（可进入安全恢复候选）',
    retire_without_successor: '剧情明确永久退场',
    replace_with_successors: '由同场后继项承接',
  }
  return labels[decision]
}

function reviewDecisionText(actionRef: string) {
  const action = reviewActionFor(actionRef)
  return action ? reviewDecisionLabel(action.decision) : ''
}

function reviewReason(actionRef: string) {
  return reviewActionFor(actionRef)?.reason || ''
}

function reviewEventIndexes(actionRef: string) {
  return reviewActionFor(actionRef)?.evidence_event_indexes.join('、') || ''
}

function reviewSuccessorNames(actionRef: string) {
  const successorRefs = reviewActionFor(actionRef)?.successor_refs || []
  return successorRefs.length ? successorNames(successorRefs) : ''
}

function successorNames(successorRefs: string[]) {
  const rows = historicalEvidence.value?.evidence.same_log_successors || []
  return successorRefs.map((successorRef) => {
    const item = rows.find((row) => row.successor_ref === successorRef)
    return item ? `${item.name}（${successorRef}）` : successorRef
  }).join('；')
}

function ambiguousReason(item: FateSettlementRepairAmbiguousItem) {
  if (item.reason === 'frozen_snapshot_missing') {
    return '冻结快照缺少原始记录，无法证明应恢复的完整数据。'
  }
  if (item.reason === 'later_structured_change') {
    return `事故后日志 #${item.later_log_id ?? '?'} 又修改过同名数据，自动归因不再唯一。`
  }
  return `未自动处理：${item.reason}`
}

function companionRiskLabel(code: string) {
  const labels: Record<string, string> = {
    continuity_binding_missing: '缺少旧伙伴与新形态的逐项审核绑定',
    continuity_binding_ambiguous: '同一旧伙伴存在多条审核绑定',
    reviewed_survivor_missing: '已审核的新形态未出现在最终新增物品中',
    reviewed_survivor_type_mismatch: '已审核的新形态不再是伙伴类型',
    reviewed_survivor_stage_regression: '已审核的新形态低于冻结阶级',
    reviewed_survivor_description_not_verbatim_preserved: '已审核的新形态丢失冻结完整描述',
    single_new_item_not_companion: '唯一同场新增物不是伙伴类型',
    single_new_companion_stage_below_departure: '唯一同场新伙伴低于出发阶级',
    single_new_item_missing_frozen_description: '唯一同场新增物丢失冻结完整描述',
    new_item_mapping_ambiguous: '同场有多个新增物，无法机械确认延续对象',
  }
  return labels[code] || code
}

function companionNewItemsLabel(item: FateSettlementCompanionContinuityRiskItem) {
  return item.new_items.map(
    (row) => `${row.name} / ${row.type || '未标类型'} / 阶级 ${row.evolution_stage}`,
  ).join('；')
}

function retryStatusLabel(status: FateSettlementRetryStatus) {
  const labels: Record<FateSettlementRetryStatus, string> = {
    reopen: '将重开',
    already_available: '已可再战',
    later_completed: '后来已成功',
    ambiguous: '保持不动',
  }
  return labels[status]
}

function retryStatusClass(status: FateSettlementRetryStatus) {
  return status === 'ambiguous' ? 'muted-tag' : ''
}

function retryStatusDescription(item: FateSettlementRetryStatusItem) {
  if (item.retry_status === 'reopen') {
    return '当前命途被错误关闭；执行后恢复到最终试炼待挑战状态，不重置已经完成的关键事件。'
  }
  if (item.retry_status === 'already_available') {
    return '当前已经可以再次挑战最终试炼，不需要写入或重置。'
  }
  if (item.retry_status === 'later_completed') {
    return `事故后已在日志 #${item.later_final_log_id ?? '?'} 成功突破，将保留现有职业与成果。`
  }
  if (item.reason === 'journey_missing') {
    return '当前找不到对应命途记录，无法安全重建，保持不动。'
  }
  if (item.reason === 'journey_replaced') {
    return `事故后的命途记录已被替换为 #${item.current_journey_id ?? '?'}，不会覆盖玩家后续选择。`
  }
  if (item.reason === 'later_journey_state_changed') {
    return `事故后命途状态已变为 ${item.current_status || '未知'}，不会自动覆盖。`
  }
  return `命途状态无法安全归因：${item.reason || '未知原因'}`
}

function parseOptionalLogId(): number | undefined {
  if (!expeditionLogIdInput.value) return undefined
  const value = Number(expeditionLogIdInput.value)
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error('探险日志 ID 必须是正整数')
  }
  return value
}

function apiErrorText(error: any, fallback: string) {
  if (error?.code === 'ECONNABORTED') {
    return '请求超过 120 秒，后端结果未知；请重新预览确认，不要直接重复执行'
  }
  return error?.response?.data?.message || error?.message || fallback
}

async function previewRepair(includeUnreviewed = false) {
  if (busy.value) return
  let logId: number | undefined
  try {
    logId = parseOptionalLogId()
  } catch (error: any) {
    message.error(error?.message || '探险日志 ID 不正确')
    return
  }
  previewLoading.value = true
  preview.value = null
  executeResult.value = null
  restoreResult.value = null
  const startedAt = performance.now()
  try {
    const response = await store.adminFateSettlementRepairPreview(
      logId,
      includeUnreviewed,
    )
    if (response.code !== 200) {
      message.error(response.message || '命途事故预览失败')
      return
    }
    preview.value = response.data as FateSettlementRepairPreview
    if (repairActionCount.value > 0) {
      message.success(
        `预览完成：${preview.value.candidate_count} 项旧数据可恢复，`
        + `${preview.value.retry_reopen_count} 人需重开最终试炼`,
      )
    } else if (preview.value.retry_already_available_count > 0) {
      message.info(
        `预览完成：${preview.value.retry_already_available_count} 人已经可以再次挑战最终试炼`,
      )
    } else if (
      preview.value.unreviewed_destructive_log_count > 0
      || preview.value.companion_continuity_audit.risk_count > 0
    ) {
      message.info(
        `扫描发现 ${preview.value.unreviewed_destructive_log_count} 条旧删除日志、`
        + `${preview.value.companion_continuity_audit.risk_count} 项伙伴替换风险，已列入只读人工核查`,
      )
    } else {
      message.info('预览完成：当前没有需要写入的自动修复')
    }
  } catch (error: any) {
    message.error(apiErrorText(error, '命途事故预览失败'))
  } finally {
    previewDurationMs.value = Math.max(0, Math.round(performance.now() - startedAt))
    previewLoading.value = false
  }
}

async function loadHistoricalReviewEvidence() {
  if (busy.value) return
  let logId: number
  try {
    const parsed = parseOptionalLogId()
    if (!parsed) throw new Error('读取证据时必须填写一条探险日志 ID')
    logId = parsed
  } catch (error: any) {
    message.error(error?.message || '探险日志 ID 不正确')
    return
  }
  evidenceLoading.value = true
  historicalEvidence.value = null
  historicalReview.value = null
  reviewPanelOpen.value = false
  preview.value = null
  executeResult.value = null
  try {
    const response = await store.adminFateSettlementReviewEvidence(logId)
    if (response.code !== 200) {
      message.error(response.message || '历史删除证据读取失败')
      return
    }
    historicalEvidence.value = response.data as FateHistoricalReviewEvidenceResponse
    reviewPanelOpen.value = true
    message.success(
      `已冻结 ${historicalEvidence.value.evidence.removed_assets.length} 项删除证据，尚未写入任何数据`,
    )
  } catch (error: any) {
    message.error(apiErrorText(error, '历史删除证据读取失败'))
  } finally {
    evidenceLoading.value = false
  }
}

async function aiReviewHistoricalEvidence() {
  const evidence = historicalEvidence.value
  if (!evidence || busy.value) return
  aiReviewLoading.value = true
  historicalReview.value = null
  preview.value = null
  executeResult.value = null
  try {
    const response = await store.adminFateSettlementAiReviewPreview(
      evidence.evidence_token,
    )
    if (response.code !== 200) {
      message.error(response.message || '历史删除 AI 判断失败')
      return
    }
    const result = response.data as FateHistoricalReviewPreviewResponse
    historicalReview.value = result.review
    preview.value = result.repair_preview
    reviewPanelOpen.value = true
    message.success(
      `AI 已完成 ${result.review.actions.length} 项判断；`
      + `${result.repair_preview.candidate_count} 项进入可执行预览，`
      + `${result.excluded_items.length} 项保持不动`,
    )
  } catch (error: any) {
    message.error(apiErrorText(error, '历史删除 AI 判断失败'))
  } finally {
    aiReviewLoading.value = false
  }
}

async function executeRepair() {
  const snapshot = preview.value
  if (!snapshot || !canExecute.value) return
  const ambiguousNote = snapshot.ambiguous_count > 0
    ? `\n另有 ${snapshot.ambiguous_count} 项证据不唯一，将保持不动。`
    : ''
  const retryAmbiguousNote = snapshot.retry_ambiguous_count > 0
    ? `\n另有 ${snapshot.retry_ambiguous_count} 人命途状态已变化，将保持不动。`
    : ''
  const unreviewedNote = snapshot.unreviewed_destructive_log_count > 0
    ? `\n另有 ${snapshot.unreviewed_destructive_log_count} 条旧删除日志仅供核查，不会自动恢复。`
    : ''
  const companionNote = snapshot.companion_continuity_audit.risk_count > 0
    ? `\n另有 ${snapshot.companion_continuity_audit.risk_count} 项伙伴替换风险仅供核查，不会自动覆盖。`
    : ''
  if (!window.confirm(
    `将恢复 ${snapshot.candidate_count} 项可由冻结快照精确证明的旧技能/旧特质，`
    + `并为 ${snapshot.retry_reopen_count} 人重开最终试炼。`
    + ambiguousNote
    + retryAmbiguousNote
    + unreviewedNote
    + companionNote
    + '\n\n不会补造职业或新成果，也不会重置已完成的关键事件。执行后请保存批次号。确定继续吗？',
  )) return

  executeLoading.value = true
  restoreResult.value = null
  try {
    const response = await store.adminFateSettlementRepairExecute(snapshot.preview_token)
    if (response.code !== 200) {
      message.error(response.message || '命途事故恢复失败')
      return
    }
    executeResult.value = response.data as FateSettlementRepairExecuteResult
    restoreRunId.value = executeResult.value.run_id
    snapshot.preview_token = ''
    message.success(
      `恢复完成：新建 ${executeResult.value.created_count} 项，`
      + `重开最终试炼 ${executeResult.value.reopened_final_count} 人`,
    )
  } catch (error: any) {
    message.error(apiErrorText(error, '命途事故恢复失败'))
  } finally {
    executeLoading.value = false
  }
}

async function restoreRepair() {
  const runId = restoreRunId.value.trim()
  if (!runId || busy.value) return
  if (!window.confirm(
    `将回滚命途恢复批次 ${runId}。\n`
    + '后端只删除该批次创建且仍保持修复原值的记录，并恢复该批次改动且之后未变化的命途状态；'
    + '后续已变更项会跳过。确定继续吗？',
  )) return

  restoreLoading.value = true
  restoreResult.value = null
  try {
    const response = await store.adminFateSettlementRepairRestore(runId)
    if (response.code !== 200) {
      message.error(response.message || '命途恢复批次回滚失败')
      return
    }
    restoreResult.value = response.data as FateSettlementRepairRestoreResult
    message.success(
      `回滚完成：删除 ${restoreResult.value.removed_count} 项，`
      + `恢复命途状态 ${restoreResult.value.restored_journey_count} 人`,
    )
  } catch (error: any) {
    message.error(apiErrorText(error, '命途恢复批次回滚失败'))
  } finally {
    restoreLoading.value = false
  }
}
</script>

<style scoped>
.fate-repair {
  display: grid;
  gap: 14px;
}

.repair-intro,
.preview-card,
.historical-review-card,
.result-card,
.restore-card {
  border: 1px solid rgba(148, 163, 184, 0.22);
  border-radius: 12px;
  background: rgba(15, 23, 42, 0.36);
}

.repair-intro {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  padding: 14px;
}

.historical-review-card {
  display: grid;
  gap: 12px;
  padding: 14px;
}

.historical-review-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.historical-review-heading p {
  margin: 6px 0 0;
  color: var(--text-secondary, #aab3c2);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.review-decision {
  color: #6ee7b7;
  font-weight: 700;
}

.muted-text {
  color: var(--text-secondary, #aab3c2);
}

.event-list {
  display: grid;
  gap: 10px;
  margin-top: 10px;
}

.event-list article {
  border: 1px solid rgba(148, 163, 184, 0.18);
  border-radius: 8px;
  padding: 10px;
}

.event-list pre {
  margin: 7px 0 0;
  color: var(--text-secondary, #cbd5e1);
  font: inherit;
  line-height: 1.65;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.repair-intro h6,
.repair-intro p,
.restore-card p,
.result-card p {
  margin: 0;
}

.repair-intro h6 {
  margin-bottom: 6px;
  font-size: 15px;
}

.repair-intro p,
.timeout-hint,
.execute-row span,
.restore-card p {
  color: var(--text-secondary, #aab3c2);
  font-size: 12px;
  line-height: 1.65;
}

.safety-badge,
.resource-tag {
  flex: none;
  border: 1px solid rgba(52, 211, 153, 0.35);
  border-radius: 999px;
  background: rgba(16, 185, 129, 0.12);
  color: #6ee7b7;
  font-size: 11px;
  font-weight: 700;
  padding: 4px 8px;
}

.scope-row,
.restore-row,
.execute-row {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  flex-wrap: wrap;
}

.scope-row label {
  display: grid;
  flex: 1 1 260px;
  gap: 5px;
  color: var(--text-secondary, #aab3c2);
  font-size: 12px;
}

.scope-row input,
.restore-row input {
  min-width: 0;
  border: 1px solid rgba(148, 163, 184, 0.3);
  border-radius: 8px;
  background: rgba(2, 6, 23, 0.55);
  color: var(--text-primary, #f8fafc);
  padding: 8px 10px;
}

.scope-row button,
.restore-row button {
  min-height: 36px;
}

.timeout-hint {
  margin: -6px 0 0;
}

.preview-card,
.result-card,
.restore-card {
  padding: 14px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 8px;
}

.metric-grid > div {
  display: grid;
  gap: 4px;
  border-radius: 9px;
  background: rgba(148, 163, 184, 0.08);
  padding: 10px;
}

.metric-grid span {
  color: var(--text-secondary, #aab3c2);
  font-size: 11px;
}

.metric-grid strong {
  font-size: 20px;
}

.metric-ready strong {
  color: #6ee7b7;
}

.metric-warning strong {
  color: #fbbf24;
}

.warning-box,
.empty-box,
.ready-box {
  margin: 12px 0 0;
  border-radius: 9px;
  padding: 9px 11px;
  font-size: 12px;
  line-height: 1.55;
}

.warning-box {
  border: 1px solid rgba(251, 191, 36, 0.28);
  background: rgba(245, 158, 11, 0.09);
  color: #fcd34d;
}

.empty-box {
  border: 1px solid rgba(52, 211, 153, 0.24);
  background: rgba(16, 185, 129, 0.08);
  color: #a7f3d0;
}

.ready-box {
  border: 1px solid rgba(96, 165, 250, 0.28);
  background: rgba(59, 130, 246, 0.09);
  color: #bfdbfe;
}

.repair-details {
  margin-top: 12px;
}

.repair-details summary {
  cursor: pointer;
  color: var(--text-secondary, #cbd5e1);
  font-size: 12px;
  font-weight: 700;
}

.repair-list {
  display: grid;
  gap: 8px;
  max-height: 360px;
  margin-top: 9px;
  overflow: auto;
}

.repair-list article {
  border-left: 2px solid rgba(52, 211, 153, 0.45);
  background: rgba(2, 6, 23, 0.35);
  padding: 9px 10px;
}

.repair-item-title {
  display: flex;
  align-items: center;
  gap: 7px;
}

.repair-list p {
  margin: 6px 0;
  color: var(--text-secondary, #aab3c2);
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
}

.repair-list small {
  color: var(--text-muted, #7c8798);
}

.muted-tag {
  border-color: rgba(251, 191, 36, 0.3);
  background: rgba(245, 158, 11, 0.1);
  color: #fcd34d;
}

.ambiguous-details .repair-list article {
  border-left-color: rgba(251, 191, 36, 0.45);
}

.excluded-details .repair-list article {
  border-left-color: rgba(148, 163, 184, 0.45);
}

.execute-row {
  align-items: center;
  margin-top: 14px;
}

.result-card {
  border-color: rgba(52, 211, 153, 0.3);
  background: rgba(16, 185, 129, 0.08);
}

.result-card p {
  margin-top: 5px;
}

.result-card code {
  overflow-wrap: anywhere;
}

.restore-card {
  display: grid;
  gap: 10px;
}

.restore-row input {
  flex: 1 1 280px;
}

.restore-result {
  color: #a7f3d0 !important;
}

@media (max-width: 720px) {
  .repair-intro {
    display: grid;
  }

  .metric-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .scope-row button,
  .restore-row button {
    width: 100%;
  }
}
</style>
