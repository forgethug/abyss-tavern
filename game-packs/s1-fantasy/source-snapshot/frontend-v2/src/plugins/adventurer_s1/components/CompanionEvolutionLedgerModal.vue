<template>
  <Teleport to="body">
    <div class="evolution-ledger-overlay s1-mobile-surface s1-mobile-overlay" @click.self="emit('close')">
      <section
        class="evolution-ledger-modal s1-mobile-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="evolution-ledger-title"
      >
        <header class="ledger-header s1-mobile-dialog-header">
          <div>
            <p class="ledger-kicker">嬗变祭坛 · 本人账目</p>
            <h3 id="evolution-ledger-title">伙伴嬗变账本</h3>
          </div>
          <button type="button" class="ledger-close" aria-label="关闭" @click="emit('close')">✕</button>
        </header>

        <div class="ledger-body s1-mobile-dialog-body">
          <p class="ledger-scope">
            账本按每一位绑定冒险者分别核对；阵亡或归档冒险者的记录不会记到后来存活的冒险者名下。
          </p>

          <div v-if="loading" class="ledger-empty">正在翻阅祭坛账册…</div>
          <div v-else-if="errorText" class="ledger-error">
            <p>{{ errorText }}</p>
            <button type="button" class="ledger-retry" @click="loadLedger">重新读取</button>
          </div>
          <template v-else-if="ledger">
            <div class="ledger-summary">
              <div>
                <span>嬗变时实付</span>
                <strong>{{ formatGold(ledger.summary.paid_at_evolution) }}</strong>
              </div>
              <div>
                <span>正确应付</span>
                <strong>{{ formatGold(ledger.summary.correct_total_due) }}</strong>
              </div>
              <div>
                <span>历史回收</span>
                <strong>{{ formatGold(ledger.summary.historical_reclaim_deducted) }}</strong>
              </div>
              <div>
                <span>后来补扣</span>
                <strong>{{ formatGold(ledger.summary.reconciliation_deducted) }}</strong>
              </div>
              <div>
                <span>后来退款</span>
                <strong>{{ formatGold(ledger.summary.reconciliation_refunded) }}</strong>
              </div>
              <div class="summary-net">
                <span>校准后累计支付</span>
                <strong>{{ formatGold(ledger.summary.net_paid) }}</strong>
              </div>
            </div>

            <p v-if="!ledger.summary.is_complete" class="ledger-warning">
              有 {{ ledger.summary.unverified_event_count }} 笔旧记录缺少精确阶级、付款或祭坛定价证据，
              汇总只计算可核实部分，不用估算冒充实付。
            </p>
            <p v-else class="ledger-balance" :class="balanceClass(ledger.summary.remaining_difference)">
              {{ balanceText(ledger.summary.remaining_difference) }}
            </p>

            <div v-if="ledger.adventurers.length === 0" class="ledger-empty">
              当前账号没有绑定冒险者。
            </div>

            <article
              v-for="(adventurer, adventurerIndex) in ledger.adventurers"
              :key="`${adventurer.adventurer_name}-${adventurer.adventurer_status}-${adventurerIndex}`"
              class="adventurer-ledger"
            >
              <header class="adventurer-ledger-head">
                <div>
                  <h4>{{ adventurer.adventurer_name }}</h4>
                  <span>{{ statusLabel(adventurer.adventurer_status) }}</span>
                </div>
                <p>
                  校准后累计支付
                  <strong>{{ formatGold(adventurer.summary.net_paid) }}</strong>
                </p>
              </header>

              <section class="current-companions">
                <h5>当前伙伴阶级</h5>
                <p v-if="adventurer.current_companions.length === 0" class="muted">
                  当前随身与收藏室没有伙伴。
                </p>
                <div v-else class="companion-list">
                  <div
                    v-for="(companion, index) in adventurer.current_companions"
                    :key="`${companion.source}-${companion.name}-${index}`"
                    class="companion-chip"
                  >
                    <strong>{{ companion.name }} · {{ companion.current_stage }}阶</strong>
                    <span>{{ companion.source === 'equipment' ? '随身' : '收藏室' }}</span>
                    <small>{{ lineageLabel(companion.lineage_evidence) }}</small>
                  </div>
                </div>
              </section>

              <section class="evolution-events">
                <h5>阶级与费用明细</h5>
                <p v-if="adventurer.events.length === 0" class="muted">
                  这位冒险者没有成功嬗变记录。交易获得的伙伴只展示当前保存的阶级，不猜测旧主人经历。
                </p>
                <div v-else class="event-list">
                  <div
                    v-for="(event, index) in adventurer.events"
                    :key="`${event.occurred_at}-${index}`"
                    class="event-card"
                  >
                    <div class="event-title">
                      <div>
                        <strong>{{ event.old_name }} → {{ event.new_name }}</strong>
                        <small>{{ formatTime(event.occurred_at) }} · {{ event.building_name }}</small>
                      </div>
                      <span class="account-badge" :class="`status-${event.account_status}`">
                        {{ accountStatusLabel(event.account_status) }}
                      </span>
                    </div>

                    <div class="stage-line">
                      <span>记录阶级：{{ event.recorded_from_stage }}阶 → {{ event.recorded_to_stage }}阶</span>
                      <span v-if="event.stage_was_corrected" class="stage-corrected">
                        核准阶级：{{ event.corrected_from_stage }}阶 → {{ event.corrected_to_stage }}阶
                      </span>
                    </div>
                    <small
                      class="lineage-note"
                      :class="{ 'lineage-unverified': !event.stage_is_verified }"
                    >
                      {{ eventLineageLabel(event) }}
                    </small>

                    <dl class="fee-grid">
                      <div>
                        <dt>当时实付</dt>
                        <dd>{{ nullableGold(event.actual_paid) }}</dd>
                      </div>
                      <div>
                        <dt>正确应付</dt>
                        <dd>{{ nullableGold(event.correct_due) }}</dd>
                      </div>
                      <div>
                        <dt>历史回收</dt>
                        <dd>{{ formatGold(event.historical_reclaim_deducted) }}</dd>
                      </div>
                      <div>
                        <dt>后来补扣</dt>
                        <dd>{{ formatGold(event.reconciliation_deducted) }}</dd>
                      </div>
                      <div>
                        <dt>后来退款</dt>
                        <dd>{{ formatGold(event.reconciliation_refunded) }}</dd>
                      </div>
                      <div>
                        <dt>最终累计支付</dt>
                        <dd>{{ nullableGold(event.total_paid_after_adjustment) }}</dd>
                      </div>
                    </dl>
                    <small v-if="event.payment_evidence === 'unavailable'" class="payment-note">
                      旧记录没有留下可唯一匹配的付款流水，因此不显示估算实付。
                    </small>
                  </div>
                </div>
              </section>
            </article>
          </template>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import api from '@/composables/useApi'

interface LedgerSummary {
  event_count: number
  paid_at_evolution: number
  correct_total_due: number
  historical_reclaim_deducted: number
  reconciliation_deducted: number
  reconciliation_refunded: number
  net_paid: number
  remaining_difference: number | null
  is_complete: boolean
  unverified_event_count: number
}

interface LedgerEvent {
  occurred_at: string | null
  building_name: string
  old_name: string
  new_name: string
  recorded_from_stage: number
  recorded_to_stage: number
  corrected_from_stage: number
  corrected_to_stage: number
  stage_is_verified: boolean
  lineage_evidence:
    | 'fee_audit'
    | 'previous_evolution'
    | 'market_transfer'
    | 'recorded_snapshot'
    | 'ambiguous_same_name'
    | 'unknown_market_transfer_stage'
    | 'unavailable'
  stage_was_corrected: boolean
  actual_paid: number | null
  payment_evidence: 'usage_log' | 'currency_log' | 'unavailable'
  correct_due: number | null
  historical_reclaim_deducted: number
  reconciliation_deducted: number
  reconciliation_refunded: number
  total_paid_after_adjustment: number | null
  remaining_difference: number | null
  account_status: 'balanced' | 'underpaid' | 'overpaid' | 'unverified'
}

interface CompanionLedger {
  adventurer_name: string
  adventurer_status: string
  current_companions: Array<{
    source: 'equipment' | 'warehouse'
    name: string
    current_stage: number
    lineage_evidence: 'own_evolution' | 'ambiguous_own_history' | 'recorded_stage_only'
  }>
  events: LedgerEvent[]
  summary: LedgerSummary
}

interface LedgerResponse {
  adventurers: CompanionLedger[]
  summary: LedgerSummary
  scope: 'owner_bound_adventurers'
}

const emit = defineEmits<{ close: [] }>()
const loading = ref(true)
const errorText = ref('')
const ledger = ref<LedgerResponse | null>(null)

function formatGold(value: number): string {
  return `${Number(value || 0).toFixed(2)} 金币`
}

function nullableGold(value: number | null): string {
  return value == null ? '证据缺失' : formatGold(value)
}

function formatTime(value: string | null): string {
  if (!value) return '时间未记录'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '时间未记录' : parsed.toLocaleString()
}

function statusLabel(status: string): string {
  if (status === 'dead') return '已阵亡'
  if (status === 'archived') return '已归档'
  return '现役'
}

function lineageLabel(evidence: string): string {
  if (evidence === 'own_evolution') return '可对应到本人的一次嬗变'
  if (evidence === 'ambiguous_own_history') return '存在多条同名记录，未强行对应'
  return '只确认当前保存阶级；交易或旧来源不作猜测'
}

function eventLineageLabel(event: LedgerEvent): string {
  if (event.lineage_evidence === 'fee_audit') {
    return `阶级来源：历史回收审计已核准为 ${event.corrected_from_stage} 阶。`
  }
  if (event.lineage_evidence === 'previous_evolution') {
    return `阶级来源：承接同一冒险者上一形态的 ${event.corrected_from_stage} 阶。`
  }
  if (event.lineage_evidence === 'market_transfer') {
    return `阶级来源：承接成交快照及其唯一前序链，核准为 ${event.corrected_from_stage} 阶。`
  }
  if (event.lineage_evidence === 'ambiguous_same_name') {
    return '阶级来源不唯一：当时存在多个同名伙伴，未强行选择其中一个，正确应付暂不判定。'
  }
  if (event.lineage_evidence === 'unknown_market_transfer_stage') {
    return '阶级来源不完整：旧成交记录没有保存可信阶级，正确应付暂不判定。'
  }
  if (event.lineage_evidence === 'recorded_snapshot') {
    return `阶级来源：采用本次嬗变前保存的 ${event.recorded_from_stage} 阶快照。`
  }
  return '阶级来源证据缺失，正确应付暂不判定。'
}

function accountStatusLabel(status: LedgerEvent['account_status']): string {
  if (status === 'balanced') return '已对平'
  if (status === 'underpaid') return '仍少付'
  if (status === 'overpaid') return '仍多付'
  return '证据不全'
}

function balanceClass(difference: number | null): string {
  if (difference == null || Math.abs(difference) < 0.01) return 'balanced'
  return difference > 0 ? 'underpaid' : 'overpaid'
}

function balanceText(difference: number | null): string {
  if (difference == null) return '账目证据不完整，无法给出最终差额。'
  if (Math.abs(difference) < 0.01) return '全部可核实嬗变费用已经对平。'
  if (difference > 0) return `全部记录合计仍少付 ${formatGold(difference)}。`
  return `全部记录合计仍多付 ${formatGold(Math.abs(difference))}。`
}

async function loadLedger() {
  loading.value = true
  errorText.value = ''
  try {
    const { data } = await api.get('/adventurer-s1/my/companion-evolution-ledger')
    if (data?.code !== 200 || !data?.data) {
      errorText.value = data?.message || '账本读取失败'
      return
    }
    ledger.value = data.data as LedgerResponse
  } catch (error: any) {
    errorText.value = error?.response?.data?.message || '账本读取失败，请稍后重试'
  } finally {
    loading.value = false
  }
}

onMounted(loadLedger)
</script>

<style scoped>
.evolution-ledger-overlay {
  position: fixed;
  inset: 0;
  z-index: 10050;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(5, 8, 12, 0.78);
  backdrop-filter: blur(4px);
}

.evolution-ledger-modal {
  width: min(1040px, 100%);
  max-height: min(90vh, 900px);
  max-height: min(90dvh, 900px);
  overflow: hidden;
  color: var(--color-text, #e8edf2);
  background: var(--color-surface, #171b21);
  border: 1px solid rgba(217, 185, 110, 0.45);
  border-radius: 16px;
  box-shadow: 0 22px 70px rgba(0, 0, 0, 0.52);
}

.ledger-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 22px;
  border-bottom: 1px solid var(--color-border, #343b45);
  background: linear-gradient(135deg, rgba(130, 92, 183, 0.18), rgba(217, 185, 110, 0.08));
}

.ledger-header h3,
.adventurer-ledger h4,
.adventurer-ledger h5,
.ledger-header p {
  margin: 0;
}

.ledger-kicker {
  margin-bottom: 3px !important;
  color: #d9b96e;
  font-size: 12px;
  letter-spacing: 0.08em;
}

.ledger-close {
  flex: none;
  width: 36px;
  height: 36px;
  color: var(--color-text, #e8edf2);
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--color-border, #343b45);
  border-radius: 9px;
  cursor: pointer;
}

.ledger-body {
  max-height: calc(min(90vh, 900px) - 74px);
  max-height: calc(min(90dvh, 900px) - 74px);
  overflow-y: auto;
  padding: 20px 22px calc(24px + env(safe-area-inset-bottom));
}

.ledger-scope,
.ledger-warning,
.ledger-balance,
.ledger-error {
  margin: 0 0 16px;
  padding: 11px 13px;
  border-radius: 9px;
}

.ledger-scope {
  color: var(--color-text-secondary, #aab3bd);
  background: rgba(130, 92, 183, 0.09);
  border: 1px solid rgba(130, 92, 183, 0.24);
}

.ledger-warning,
.ledger-balance.underpaid,
.ledger-balance.overpaid {
  color: #f1cf8c;
  background: rgba(198, 132, 46, 0.12);
  border: 1px solid rgba(198, 132, 46, 0.35);
}

.ledger-balance.balanced {
  color: #91d7ac;
  background: rgba(53, 154, 94, 0.12);
  border: 1px solid rgba(53, 154, 94, 0.32);
}

.ledger-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}

.ledger-summary > div {
  display: flex;
  min-height: 70px;
  flex-direction: column;
  justify-content: center;
  gap: 5px;
  padding: 11px 13px;
  background: var(--color-bg, #11161b);
  border: 1px solid var(--color-border, #343b45);
  border-radius: 10px;
}

.ledger-summary span,
.fee-grid dt {
  color: var(--color-text-secondary, #aab3bd);
  font-size: 12px;
}

.ledger-summary strong {
  color: #e2c57e;
  font-size: 16px;
}

.ledger-summary .summary-net {
  background: rgba(217, 185, 110, 0.08);
  border-color: rgba(217, 185, 110, 0.34);
}

.adventurer-ledger {
  margin-top: 18px;
  overflow: hidden;
  border: 1px solid var(--color-border, #343b45);
  border-radius: 12px;
}

.adventurer-ledger-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 14px 16px;
  background: rgba(255, 255, 255, 0.025);
  border-bottom: 1px solid var(--color-border, #343b45);
}

.adventurer-ledger-head div {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.adventurer-ledger-head span,
.adventurer-ledger-head p {
  margin: 0;
  color: var(--color-text-secondary, #aab3bd);
  font-size: 12px;
}

.adventurer-ledger-head strong {
  color: #e2c57e;
}

.current-companions,
.evolution-events {
  padding: 15px 16px;
}

.current-companions {
  border-bottom: 1px solid var(--color-border, #343b45);
}

.current-companions h5,
.evolution-events h5 {
  margin-bottom: 10px;
}

.companion-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.companion-chip {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 3px 10px;
  padding: 10px 12px;
  background: rgba(130, 92, 183, 0.08);
  border: 1px solid rgba(130, 92, 183, 0.25);
  border-radius: 9px;
}

.companion-chip span {
  color: #c7a9ed;
  font-size: 12px;
}

.companion-chip small {
  grid-column: 1 / -1;
  color: var(--color-text-secondary, #aab3bd);
}

.event-list {
  display: grid;
  gap: 10px;
}

.event-card {
  padding: 13px;
  background: var(--color-bg, #11161b);
  border: 1px solid var(--color-border, #343b45);
  border-radius: 10px;
}

.event-title {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.event-title div {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}

.event-title small,
.payment-note,
.muted {
  color: var(--color-text-secondary, #aab3bd);
}

.account-badge {
  flex: none;
  padding: 3px 8px;
  font-size: 11px;
  border-radius: 999px;
}

.status-balanced {
  color: #91d7ac;
  background: rgba(53, 154, 94, 0.16);
}

.status-underpaid,
.status-overpaid,
.status-unverified {
  color: #f1cf8c;
  background: rgba(198, 132, 46, 0.16);
}

.stage-line {
  display: flex;
  flex-wrap: wrap;
  gap: 7px 14px;
  margin: 10px 0;
  font-size: 13px;
}

.stage-corrected {
  color: #d9b96e;
  font-weight: 700;
}

.lineage-note {
  display: block;
  margin: -3px 0 10px;
  color: #aebfd4;
}

.lineage-unverified {
  color: #f1cf8c;
}

.fee-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 7px;
  margin: 0;
}

.fee-grid div {
  padding: 8px 9px;
  background: rgba(255, 255, 255, 0.025);
  border-radius: 7px;
}

.fee-grid dd {
  margin: 3px 0 0;
  font-size: 13px;
  font-weight: 700;
}

.payment-note {
  display: block;
  margin-top: 8px;
}

.ledger-empty,
.ledger-error {
  padding: 34px 12px;
  text-align: center;
  color: var(--color-text-secondary, #aab3bd);
}

.ledger-error {
  color: #efaaaa;
  border: 1px solid rgba(206, 76, 76, 0.32);
}

.ledger-retry {
  margin-top: 8px;
  padding: 7px 14px;
  color: var(--color-text, #e8edf2);
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid var(--color-border, #343b45);
  border-radius: 7px;
  cursor: pointer;
}

@media (max-width: 720px) {
  .evolution-ledger-overlay {
    align-items: flex-end;
    padding: 0;
  }

  .evolution-ledger-modal {
    width: 100%;
    max-height: 94vh;
    max-height: 94dvh;
    border-radius: 16px 16px 0 0;
  }

  .ledger-body {
    max-height: calc(94dvh - 70px);
    padding: 15px 14px calc(18px + env(safe-area-inset-bottom));
  }

  .ledger-summary,
  .fee-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .companion-list {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 440px) {
  .ledger-header,
  .adventurer-ledger-head,
  .event-title {
    align-items: flex-start;
  }

  .adventurer-ledger-head,
  .event-title {
    flex-direction: column;
  }

  .ledger-summary,
  .fee-grid {
    grid-template-columns: 1fr;
  }
}
</style>
