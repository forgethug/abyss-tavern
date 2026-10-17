<template>
  <div class="insurance-panel">
    <div class="insurance-header">
      <h5 class="insurance-title">保险局 🛡️</h5>
      <span class="insurance-hint">保单可同时购买 · 下次探险结算后分别判定并消耗</span>
    </div>

    <div v-if="!myAdv" class="state-msg">你还没有存活的冒险者</div>
    <template v-else>
      <p v-if="isExploring" class="exploring-block-hint">
        你的冒险者正在探险中，暂无法购买保险。
      </p>
      <div class="insured-adventurer">
        <span>投保冒险者</span>
        <strong>{{ myAdv.name }}</strong>
      </div>

      <div class="policy-list">
        <article v-for="policy in policies" :key="policy.code" class="policy-card">
          <div class="policy-heading">
            <div>
              <h6>{{ policy.name }}</h6>
              <span :class="['policy-status', { active: policy.active }]">
                {{ policy.active ? '已投保' : '未投保' }}
              </span>
            </div>
            <strong>{{ policy.premium }} 金币 / 次</strong>
          </div>

          <p class="policy-description">{{ policy.description }}</p>
          <div class="policy-payout">
            <span>{{ policy.code === 'injury' ? '最高赔付' : '触发赔付' }}</span>
            <strong>{{ policy.max_payout ?? policy.payout ?? 0 }} 金币</strong>
          </div>

          <div v-if="policy.code === 'injury' && policy.payout_samples?.length" class="injury-samples">
            <div
              v-for="sample in policy.payout_samples"
              :key="`${sample.label}-${sample.payout}`"
              class="sample-row"
            >
              <span>{{ sample.label }}</span>
              <strong>{{ sample.payout }} 金币</strong>
            </div>
          </div>

          <button
            type="button"
            class="insurance-buy-btn"
            :disabled="loading || isExploring || policy.active || actingPolicy !== null"
            @click="buy(policy)"
          >
            {{ policy.active ? '已投保' : actingPolicy === policy.code ? '购买中…' : `购买${policy.name}` }}
          </button>
        </article>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  useAdventurerS1Store,
  type InsurancePolicyStatus,
  type InsuranceStatus,
} from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { isExploringStatus, EXPLORING_BLOCKED_HINT } from '../utils'

const store = useAdventurerS1Store()
const message = useGlobalMessage()

const loading = ref(false)
const actingPolicy = ref<InsurancePolicyStatus['code'] | null>(null)
const status = ref<InsuranceStatus | null>(null)

const myAdv = computed(() => store.adventurers.find(a => a.status !== 'dead' && a.status !== 'archived') ?? null)
const isExploring = computed(() => isExploringStatus(myAdv.value?.status))

const fallbackPolicies: InsurancePolicyStatus[] = [
  {
    code: 'injury',
    name: '受伤保险',
    description: '生还时按本次损失生命占最大生命的比例非线性赔付。',
    premium: 10,
    max_payout: 500,
    active: false,
    payout_samples: [
      { loss_pct: 30, payout: 25, label: '损失30%' },
      { loss_pct: 50, payout: 88, label: '损失50%' },
      { loss_pct: 70, payout: 205, label: '损失70%' },
      { loss_pct: 90, payout: 384, label: '损失90%' },
      { loss_pct: 100, payout: 500, label: '损失100%' },
    ],
  },
  {
    code: 'empty_return',
    name: '空手归途险',
    description: '普通探险中生还但无正金币、无新物品、无新技能或特质、无职业或属性成长时赔付；特殊机缘副本不赔付。',
    premium: 100,
    payout: 500,
    active: false,
  },
  {
    code: 'comrade_memorial',
    name: '同袍抚恤险',
    description: '投保者生还且本次至少一名其他队友阵亡时赔付。',
    premium: 100,
    payout: 800,
    active: false,
  },
  {
    code: 'no_gold',
    name: '零钱兜底险',
    description: '普通副本中没有获得正金币收益时赔付；特殊机缘副本不赔付。',
    premium: 10,
    payout: 50,
    active: false,
  },
  {
    code: 'no_item',
    name: '空囊补偿险',
    description: '普通副本中没有获得任何物品或材料时赔付；特殊机缘副本不赔付。',
    premium: 10,
    payout: 50,
    active: false,
  },
]

const policies = computed(() => status.value?.policies?.length ? status.value.policies : fallbackPolicies)

async function loadStatus() {
  if (!myAdv.value) return
  loading.value = true
  try {
    const res = await store.fetchInsuranceStatus(myAdv.value.id)
    if (res.code === 200) {
      status.value = res.data
    } else {
      message.error(res.message || '加载保险状态失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加载保险状态失败')
  } finally {
    loading.value = false
  }
}

async function buy(policy: InsurancePolicyStatus) {
  if (!myAdv.value || actingPolicy.value !== null || isExploring.value) {
    if (isExploring.value) message.error(EXPLORING_BLOCKED_HINT)
    return
  }
  actingPolicy.value = policy.code
  try {
    const res = await store.buyInsurance(myAdv.value.id, policy.code)
    if (res.code === 200) {
      message.success(`${policy.name}购买成功，将在下次探险结算后自动判定`)
      await loadStatus()
    } else {
      message.error(res.message || `购买${policy.name}失败`)
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || `购买${policy.name}失败`)
  } finally {
    actingPolicy.value = null
  }
}

onMounted(async () => {
  if (store.adventurers.length === 0) {
    await store.fetchAdventurers()
  }
  await loadStatus()
})
</script>

<style scoped>
.insurance-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.insurance-header {
  text-align: center;
}

.insurance-title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-text);
}

.insurance-hint,
.state-msg {
  font-size: 12px;
  color: var(--color-text-secondary, #888);
}

.state-msg {
  padding: 20px;
  text-align: center;
}

.insured-adventurer,
.policy-payout,
.sample-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.insured-adventurer {
  padding: 10px 12px;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 10px;
}

.policy-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 230px), 1fr));
  gap: 12px;
}

.policy-card {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 10px;
}

.policy-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.policy-heading h6 {
  margin: 0 0 4px;
  font-size: 15px;
}

.policy-status {
  font-size: 12px;
  color: var(--color-text-secondary, #888);
}

.policy-status.active {
  color: var(--color-success, #198754);
}

.policy-description {
  min-height: 42px;
  margin: 0;
  overflow-wrap: anywhere;
  font-size: 13px;
  line-height: 1.6;
  color: var(--color-text-secondary, #888);
}

.injury-samples {
  padding-top: 6px;
  border-top: 1px solid var(--color-border);
}

.sample-row {
  padding: 2px 0;
  font-size: 12px;
}

.insurance-buy-btn {
  margin-top: auto;
  align-self: flex-start;
  padding: 6px 14px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: #348b6c;
  background: var(--color-primary, #348b6c);
  color: #fff;
  cursor: pointer;
}

.insurance-buy-btn:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.exploring-block-hint {
  margin: 0;
  padding: 8px 12px;
  border: 1px solid color-mix(in srgb, var(--color-warning, #e6a817) 35%, var(--color-border));
  border-radius: 8px;
  background: color-mix(in srgb, var(--color-warning, #e6a817) 12%, var(--color-surface));
  color: var(--color-text);
  font-size: 13px;
}
@media (max-width: 640px) {
  .policy-list { grid-template-columns: minmax(0, 1fr); }
  .insured-adventurer, .policy-heading, .policy-payout, .sample-row { flex-wrap: wrap; gap: 6px; }
  .policy-description { min-height: 0; font-size: 13px; line-height: 1.6; }
  .insurance-buy-btn { width: 100%; }
  .insurance-hint, .sample-row { font-size: 13px; line-height: 1.6; }
}
</style>
