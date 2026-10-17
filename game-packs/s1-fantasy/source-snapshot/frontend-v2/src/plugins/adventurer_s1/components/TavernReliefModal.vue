<template>
  <Teleport to="body">
    <div class="relief-mask s1-mobile-surface s1-mobile-overlay" @click.self="closeModal">
      <section class="relief-card s1-mobile-dialog" role="dialog" aria-modal="true" aria-labelledby="tavern-relief-title">
        <header class="relief-head s1-mobile-dialog-header">
          <div>
            <p class="relief-kicker">数据申诉 · 善后补贴</p>
            <h3 id="tavern-relief-title">🍺 酒馆补贴申请</h3>
          </div>
          <button
            type="button"
            class="relief-close"
            :disabled="claiming"
            @click="closeModal"
          >✕</button>
        </header>

        <div class="relief-body s1-mobile-dialog-body">
          <p class="relief-intro">
            相比普通申诉，酒馆补贴申请更容易通过，获批时补贴也会更高。
            可在每日额度内重复申请；酒馆只按同一趟探险的历史最高目标补足差额。
            每次有效核定会使用 1 次今日申诉次数。
          </p>

          <div v-if="loading" class="relief-state">正在读取最近一次探险…</div>
          <div v-else-if="errorText" class="relief-state error">{{ errorText }}</div>
          <template v-else-if="preview">
            <div v-if="preview.expedition" class="expedition-box">
              <strong>{{ preview.expedition.target_dungeon || '最近一次探险' }}</strong>
              <span v-if="preview.expedition.party_name">{{ preview.expedition.party_name }}</span>
              <small v-if="preview.expedition.created_at">
                {{ formatDateTime(preview.expedition.created_at) }}
              </small>
            </div>

            <div v-if="preview.decision === 'approve'" class="amount-grid">
              <div>
                <span>核定补贴</span>
                <strong>{{ formatGold(preview.target_per_person) }} / 人</strong>
              </div>
              <div>
                <span>覆盖队员</span>
                <strong>{{ preview.recipient_count }} 人</strong>
              </div>
              <div>
                <span>团队目标</span>
                <strong>{{ formatGold(preview.team_target_total) }} 金币</strong>
              </div>
              <div>
                <span>申请者额外补贴</span>
                <strong>
                  {{ formatGold(preview.applicant_bonus_grant) }} 金币
                </strong>
              </div>
              <div>
                <span>本次总到账</span>
                <strong>{{ formatGold(preview.grant_total) }} 金币</strong>
              </div>
            </div>

            <p
              :class="[
                'relief-result',
                {
                  approved: preview.decision === 'approve',
                  rejected: preview.decision === 'reject',
                },
              ]"
            >
              {{ preview.message || '核定完成' }}
            </p>

            <p
              v-if="reliefQuota"
              :class="['quota-state', { exhausted: reliefQuota.remaining <= 0 }]"
            >
              今日其余申诉额度还剩 {{ reliefQuota.unlimited ? '不限' : reliefQuota.remaining }} 次
              <span>（已用 {{ reliefQuota.used_24h }} / {{ reliefQuota.unlimited ? '不限' : reliefQuota.limit }}）</span>
            </p>

            <ul
              v-if="preview.decision === 'approve' && preview.recipients?.length"
              class="recipient-list"
            >
              <li v-for="row in preview.recipients" :key="row.name">
                <span>{{ row.name }}</span>
                <span>
                  目标 {{ formatGold(row.target_amount) }}
                  <template v-if="row.previously_covered > 0">
                    · 已覆盖 {{ formatGold(row.previously_covered) }}
                  </template>
                  · 本次 +{{ formatGold(row.grant_amount) }}
                </span>
              </li>
            </ul>
          </template>
        </div>

        <footer class="relief-actions s1-mobile-dialog-footer">
          <button
            type="button"
            class="btn-secondary btn-sm"
            :disabled="claiming"
            @click="closeModal"
          >关闭</button>
          <button
            v-if="preview?.claimable"
            type="button"
            class="btn-primary btn-sm"
            :disabled="claiming || submittedForSession || (reliefQuota?.remaining ?? 0) <= 0"
            @click="claim"
          >{{
            claiming
              ? '核定中…'
              : submittedForSession
                ? '本次申请已提交'
              : (reliefQuota?.remaining ?? 0) <= 0
                ? '今日其余申诉次数已用完'
                : `提交酒馆补贴申请`
          }}</button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { formatDateTime } from '@/utils/time'
import { useAdventurerS1Store } from '../stores/adventurerS1'

interface TavernReliefPreview {
  enabled: boolean
  eligible: boolean
  claimable: boolean
  already_covered: boolean
  appeal_id?: number
  request_status?: 'processing' | 'done' | 'failed'
  request_reused?: boolean
  decision: 'pending' | 'approve' | 'reject'
  expedition: {
    id: number
    target_dungeon: string
    party_name?: string | null
    created_at?: string | null
  } | null
  target_per_person: number
  max_target_per_person: number
  payout_per_person: number
  recipient_count: number
  team_target_total: number
  team_grant_total: number
  applicant_bonus_target: number
  max_applicant_bonus_target: number
  applicant_bonus_grant: number
  applicant_bonus: {
    name: string
    target_amount: number
    previously_covered: number
    grant_amount: number
  } | null
  grant_total: number
  quota: {
    standard?: {
      remaining: number
      limit: number
      unlimited?: boolean
  used_24h: number
    }
    remaining: number
    limit: number
    unlimited?: boolean
  used_24h: number
  }
  recipients: Array<{
    name: string
    target_amount: number
    previously_covered: number
    grant_amount: number
  }>
  message: string
}

const emit = defineEmits<{ close: []; claimed: [] }>()
const store = useAdventurerS1Store()
const message = useGlobalMessage()
const loading = ref(true)
const claiming = ref(false)
const submittedForSession = ref(false)
const errorText = ref('')
const preview = ref<TavernReliefPreview | null>(null)
const activeAppealId = ref<number | null>(null)
const reliefQuota = computed(() => (
  preview.value?.quota?.standard || preview.value?.quota || null
))
function createClientRequestId() {
  return (
    globalThis.crypto?.randomUUID?.()
    || `tavern-relief-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  )
}

const clientRequestId = ref(createClientRequestId())
let pollTimer: ReturnType<typeof setInterval> | null = null
let pollInFlight = false

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function applyRequestPreview(data: TavernReliefPreview, notifyCompletion = false) {
  preview.value = data
  activeAppealId.value = data.appeal_id || null
  if (data.request_status === 'processing') {
    submittedForSession.value = true
    startPolling()
    return
  }

  stopPolling()
  if (!notifyCompletion || !data.request_status) return
  if (data.request_status === 'failed') {
    submittedForSession.value = false
    activeAppealId.value = null
    clientRequestId.value = createClientRequestId()
    message?.error(data.message || '酒馆核定暂时未完成，请重新提交申请')
    return
  }
  submittedForSession.value = true
  if (data.decision === 'approve') {
    message?.success(data.message || '酒馆补贴已经到账')
    emit('claimed')
  } else {
    message?.warning(data.message || '酒馆已完成核定')
  }
}

async function pollRequestStatus() {
  if (!activeAppealId.value || pollInFlight) {
    if (!activeAppealId.value) stopPolling()
    return
  }
  pollInFlight = true
  try {
    const res = await store.fetchTavernReliefPreview(
      clientRequestId.value,
      activeAppealId.value,
    )
    if (res?.code === 200 && res.data) {
      applyRequestPreview(res.data, res.data.request_status !== 'processing')
    }
  } catch {
    // 后台任务状态轮询失败不等于核定或发放失败；保留下一轮重试。
  } finally {
    pollInFlight = false
  }
}

function startPolling() {
  if (pollTimer) return
  pollTimer = setInterval(pollRequestStatus, 5000)
}

function formatGold(value: number | null | undefined) {
  const amount = Number(value || 0)
  return Number.isInteger(amount) ? String(amount) : amount.toFixed(2)
}

async function loadPreview() {
  loading.value = true
  errorText.value = ''
  try {
    const res = await store.fetchTavernReliefPreview(clientRequestId.value)
    if (res?.code === 200) {
      applyRequestPreview(res.data)
    } else {
      errorText.value = res?.message || '核定失败'
    }
  } catch (error: any) {
    errorText.value = error?.response?.data?.message || '核定失败，请稍后重试'
  } finally {
    loading.value = false
  }
}

async function claim() {
  if (claiming.value || submittedForSession.value || !preview.value?.claimable) return
  claiming.value = true
  try {
    const res = await store.claimTavernRelief(clientRequestId.value)
    if (res?.code === 200) {
      applyRequestPreview(res.data)
      submittedForSession.value = true
      const resultMessage = res.message || res.data?.message || '酒馆补贴申请已提交'
      if (res.data?.request_status === 'processing') {
        message?.success(resultMessage)
      } else if (res.data?.request_reused) {
        message?.warning(resultMessage)
        if (res.data?.decision === 'approve') emit('claimed')
      } else if (res.data?.decision === 'approve') {
        message?.success(resultMessage)
        emit('claimed')
      } else {
        message?.warning(resultMessage)
      }
    } else {
      message?.error(res?.message || '发放失败')
    }
  } catch (error: any) {
    message?.error(error?.response?.data?.message || '发放失败，请稍后重试')
  } finally {
    claiming.value = false
  }
}

function closeModal() {
  if (claiming.value) return
  emit('close')
}

onMounted(loadPreview)
onUnmounted(stopPolling)
</script>

<style scoped lang="scss">
.relief-mask {
  position: fixed;
  inset: 0;
  z-index: 1120;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: rgba(0, 0, 0, 0.58);
}

.relief-card {
  width: min(520px, 100%);
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid rgba(208, 161, 71, 0.55);
  border-radius: 14px;
  background: var(--color-surface);
}

.relief-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 18px;
  border-bottom: 1px solid var(--color-border);

  h3 {
    margin: 2px 0 0;
    color: var(--color-text);
    font-size: 18px;
  }
}

.relief-kicker {
  margin: 0;
  color: #c9912d;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.relief-close {
  border: 0;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 18px;

  &:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }
}

.relief-body {
  overflow-y: auto;
  padding: 18px;
}

.relief-intro {
  margin: 0 0 14px;
  color: var(--color-text-secondary);
  font-size: 13px;
  line-height: 1.6;
}

.relief-state,
.relief-result {
  padding: 14px;
  border-radius: 8px;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  font-size: 13px;

  &.error {
    color: #d03050;
  }

  &.approved {
    color: #b7791f;
    background: rgba(208, 161, 71, 0.12);
  }

  &.rejected {
    color: #b42318;
    background: rgba(180, 35, 24, 0.08);
  }
}

.quota-state {
  margin: 10px 0 0;
  color: var(--color-text-secondary);
  font-size: 12px;

  span {
    opacity: 0.78;
  }

  &.exhausted {
    color: #d03050;
  }
}

.expedition-box {
  display: grid;
  gap: 3px;
  margin-bottom: 12px;
  padding: 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;

  span,
  small {
    color: var(--color-text-secondary);
  }
}

.amount-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 12px;

  div {
    display: grid;
    gap: 4px;
    padding: 10px;
    border-radius: 8px;
    background: var(--color-bg);
  }

  span {
    color: var(--color-text-secondary);
    font-size: 11px;
  }

  strong {
    color: var(--color-text);
    font-size: 14px;
  }
}

.recipient-list {
  display: grid;
  gap: 6px;
  margin: 12px 0 0;
  padding: 0;
  list-style: none;

  li {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 10px;
    border-bottom: 1px solid var(--color-border);
    color: var(--color-text);
    font-size: 12px;

    span:last-child {
      color: var(--color-text-secondary);
      text-align: right;
    }
  }
}

.relief-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 18px;
  border-top: 1px solid var(--color-border);
}

@media (max-width: 520px) {
  .amount-grid {
    grid-template-columns: 1fr;
  }

  .recipient-list li {
    display: grid;
  }
}
</style>
