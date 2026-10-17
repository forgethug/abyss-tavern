<template>
  <div class="skill-school-panel">
    <div class="skill-school-header">
      <h5 class="section-title">📜 百艺阁</h5>
      <div v-if="catalog?.adventurer" class="skill-school-wallet">
        {{ catalog.adventurer.name }} · 💰 {{ catalog.adventurer.gold }} 金币
      </div>
    </div>

    <div class="mode-tabs" role="tablist" aria-label="百艺阁传艺方式">
      <button
        type="button"
        class="mode-tab"
        :class="{ active: mode === 'classic' }"
        @click="mode = 'classic'"
      >
        旧卷传艺
      </button>
      <button
        type="button"
        class="mode-tab"
        :class="{ active: mode === 'custom' }"
        @click="mode = 'custom'"
      >
        请导师指点<span v-if="retryOpportunity">（有补课）</span>
      </button>
    </div>

    <div v-if="loading" class="empty-helpers">正翻找阁里的旧卷…</div>
    <div v-else-if="loadError" class="empty-helpers error-state">
      <span>{{ loadError }}</span>
      <button type="button" class="btn-secondary btn-sm" @click="loadCatalog">重试</button>
    </div>
    <div v-else-if="!catalog?.adventurer" class="empty-helpers">
      你还没有能来学艺的冒险者
    </div>

    <template v-else-if="mode === 'classic'">
      <p class="skill-school-hint">
        阁里的旧卷记着深渊酒馆流传已久的招式。跟导师练上一回要 {{ catalog.usage_duration_minutes }} 分钟；
        阁里摆着 {{ catalog.helper_slots }} 张传艺席。
        <template v-if="catalog.helper_slots > 0">
          眼下坐了 {{ catalog.current_users }}/{{ catalog.helper_slots }} 人。
        </template>
      </p>

      <div class="skill-school-list">
        <div
          v-for="skill in catalog.skills"
          :key="skill.code"
          class="skill-school-card"
          :class="{ learned: skill.learned, disabled: !skill.can_learn && !skill.learned }"
        >
          <div class="skill-school-card-head">
            <span class="skill-name">{{ skill.name }}</span>
            <span v-if="skill.learned" class="skill-badge learned-badge">已经会了</span>
            <span v-else class="skill-badge price-badge">💰 {{ skill.gold_cost }}</span>
          </div>
          <div v-if="skill.description" class="skill-desc">{{ skill.description }}</div>
          <div class="skill-school-card-foot">
            <button
              v-if="!skill.learned"
              type="button"
              class="btn-primary btn-sm"
              :disabled="!skill.can_learn || submitting"
              @click="openClassicConfirm(skill)"
            >
              跟着旧卷练
            </button>
            <span v-else class="learned-text">✅ 这招已会</span>
            <span v-if="!skill.learned && !skill.can_learn && skill.reject_reason" class="reject-reason">
              {{ skill.reject_reason }}
            </span>
          </div>
        </div>
      </div>
    </template>

    <template v-else-if="customTraining">
      <p class="skill-school-hint">
        把你想练成的本事告诉导师。能照着教，导师便顺着这条路带你练；若路子不合身，
        也会看看你的职业、有效特质与已有本事，留住心意，改成真正使得出的招。
      </p>

      <div class="training-facts" aria-label="单独指点所需">
        <span>⏱ {{ customTraining.usage_duration_minutes }} 分钟</span>
        <span>⚡ {{ customTraining.stamina_cost }} 精力</span>
        <span v-if="customTraining.helper_slots > 0">
          👥 导师正指点 {{ customTraining.current_users }}/{{ customTraining.helper_slots }} 人
        </span>
        <span>🚶 候场 {{ customTraining.queue.waiting_count }} 人</span>
        <span>🕒 开过一次小灶后，导师要歇 {{ customTraining.cooldown_hours }} 小时</span>
      </div>

      <div v-if="retryOpportunity" class="retry-opportunity">
        <div class="retry-opportunity-head">
          <div>
            <strong>导师替你留着上回没授成的补课</strong>
            <span>原课堂已经记档，本次不用重新排队，也不用再等 {{ retryOpportunity.usage_duration_minutes ?? customTraining.usage_duration_minutes }} 分钟。</span>
          </div>
          <span class="skill-badge retry-badge">原样保留</span>
        </div>
        <dl v-if="retryOpportunity.operation" class="retry-details">
          <div>
            <dt>研修方式</dt>
            <dd>{{ retryOpportunity.operation === 'learn' ? '另学一门本事' : '把旧招再磨一层' }}</dd>
          </div>
          <div v-if="retryOpportunity.target_skill">
            <dt>原定旧招</dt>
            <dd>
              <strong>{{ retryOpportunity.target_skill.name }}</strong>
              <span>{{ retryOpportunity.target_skill.description }}</span>
            </dd>
          </div>
          <div>
            <dt>原练法</dt>
            <dd>{{ retryOpportunity.direction }}</dd>
          </div>
          <div>
            <dt>本次所需</dt>
            <dd>💰 {{ retryOpportunity.gold_cost ?? 0 }} 金币 · ⚡ {{ retryOpportunity.stamina_cost ?? 0 }} 精力</dd>
          </div>
        </dl>
        <div class="custom-submit-row">
          <button
            type="button"
            class="btn-primary"
            :disabled="submitting || !retryOpportunity.can_retry"
            @click="openRetryConfirm"
          >
            {{ submitting ? '正请导师补课…' : '按上回练法直接补课' }}
          </button>
          <span v-if="retryOpportunity.reject_reason" class="reject-reason">
            {{ retryOpportunity.reject_reason }}
          </span>
        </div>
      </div>

      <template v-else>
      <div v-if="customTraining.queue.is_waiting" class="queue-notice">
        <div>
          <strong>已在导师候场队列第 {{ customTraining.queue.position }} 位</strong>
          <span>候场期间不会恢复精力，也不能进行其他行动；前一位离开后会自动入席。</span>
        </div>
        <button
          type="button"
          class="btn-secondary btn-sm"
          :disabled="cancellingQueue"
          @click="cancelQueue"
        >
          {{ cancellingQueue ? '正在取消…' : '取消排队' }}
        </button>
      </div>

      <div v-if="customTraining.queue.is_waiting" class="queued-request">
        <template v-if="customTraining.queued_request">
          <strong>已提交的练法 · {{ customTraining.queued_request.operation === 'advance' ? '把旧招再磨一层' : '另学一门本事' }}</strong>
          <p v-if="customTraining.queued_request.target_skill_name">磨炼招式：{{ customTraining.queued_request.target_skill_name }}</p>
          <p class="queued-direction">{{ customTraining.queued_request.direction || '未填写练法' }}</p>
        </template>
        <p v-else>暂时无法预览这次练法。</p>
        <small>如需修改，请先取消排队，再重新提交；重新排队会从队尾开始。</small>
      </div>

      <template v-else>
      <div v-if="customCooldownRemaining > 0" class="cooldown-notice">
        导师刚替你开过一回小灶，还需歇 {{ formatDurationSec(customCooldownRemaining) }}
      </div>

      <div class="operation-grid">
        <button
          type="button"
          class="operation-card"
          :class="{ active: customOperation === 'learn' }"
          @click="customOperation = 'learn'"
        >
          <strong>另学一门本事</strong>
          <span>💰 {{ customTraining.operation_config.learn.gold_cost }}</span>
        </button>
        <button
          type="button"
          class="operation-card"
          :class="{ active: customOperation === 'advance' }"
          @click="customOperation = 'advance'"
        >
          <strong>把旧招再磨一层</strong>
          <span>💰 {{ customTraining.operation_config.advance.gold_cost }}</span>
        </button>
      </div>

      <label v-if="customOperation === 'advance'" class="field-label">
        想继续磨炼哪一招
        <select v-model.number="targetSkillId" class="skill-select" :disabled="submitting">
          <option :value="null" disabled>先挑一门旧招</option>
          <option v-for="skill in customTraining.skills" :key="skill.id" :value="skill.id">
            {{ skill.name }}{{ skill.equipped ? '（已携带）' : '' }}
          </option>
        </select>
      </label>

      <label class="field-label">
        想往哪条路练
        <textarea
          v-model="direction"
          class="direction-input"
          :maxlength="customTraining.direction_max_length"
          :disabled="submitting"
          placeholder="比如：想在乱战里护住身后的同伴，也能绊住贴身的敌人"
          rows="4"
        />
        <span class="char-count">{{ direction.length }}/{{ customTraining.direction_max_length }}</span>
      </label>

      <div class="custom-submit-row">
        <button
          type="button"
          class="btn-primary"
          :disabled="!canSubmitCustom"
          @click="openCustomConfirm"
        >
          {{ customSubmitLabel }}
        </button>
        <span v-if="customRejectReason" class="reject-reason">{{ customRejectReason }}</span>
      </div>
      </template>
      </template>
    </template>

    <S1ConfirmDialog
      v-if="pendingConfirm"
      :title="confirmTitle"
      :message="confirmMessage"
      :busy="submitting"
      :confirm-label="confirmLabel"
      @confirm="confirmStart"
      @cancel="pendingConfirm = null"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { useLiveTick } from '../composables/useLiveTick'
import { formatDurationSec } from '../utils'
import S1ConfirmDialog from './S1ConfirmDialog.vue'
import {
  useAdventurerS1Store,
  type SkillSchoolCatalog,
  type SkillSchoolOffer,
  type SkillSchoolOwnedSkill,
} from '../stores/adventurerS1'

const props = defineProps<{ building: { id: number } }>()
const emit = defineEmits<{ refreshed: [] }>()

type Operation = 'learn' | 'advance'
type PendingConfirm =
  | { kind: 'classic'; skill: SkillSchoolOffer }
  | { kind: 'retry' }
  | {
      kind: 'custom'
      operation: Operation
      direction: string
      targetSkill: SkillSchoolOwnedSkill | null
      joinQueue: boolean
    }

const store = useAdventurerS1Store()
const message = useMessage()
const { now } = useLiveTick()

const loading = ref(false)
const loadError = ref('')
const submitting = ref(false)
const cancellingQueue = ref(false)
const catalog = ref<SkillSchoolCatalog | null>(null)
const mode = ref<'classic' | 'custom'>('classic')
const customOperation = ref<Operation>('learn')
const direction = ref('')
const targetSkillId = ref<number | null>(null)
const pendingConfirm = ref<PendingConfirm | null>(null)
const cooldownDeadlineMs = ref(0)
const cooldownReloaded = ref(false)

const customTraining = computed(() => catalog.value?.custom_training ?? null)
const retryOpportunity = computed(() => customTraining.value?.retry_opportunity ?? null)
const mentorRequiresQueue = computed(() => {
  const custom = customTraining.value
  return Boolean(
    custom
    && (
      custom.queue.waiting_count > 0
      || (
        custom.helper_slots > 0
        && custom.current_users >= custom.helper_slots
      )
    ),
  )
})
const customCooldownRemaining = computed(() => (
  cooldownDeadlineMs.value > now.value
    ? Math.ceil((cooldownDeadlineMs.value - now.value) / 1000)
    : 0
))
const selectedTargetSkill = computed(() => (
  customTraining.value?.skills.find(skill => skill.id === targetSkillId.value) ?? null
))
const operationConfig = computed(() => customTraining.value?.operation_config[customOperation.value] ?? null)
const customRejectReason = computed(() => {
  if (customCooldownRemaining.value > 0) {
    return `导师还要歇 ${formatDurationSec(customCooldownRemaining.value)}`
  }
  if (customOperation.value === 'advance' && !customTraining.value?.skills.length) {
    return '还没有能继续磨炼的旧招'
  }
  return operationConfig.value?.reject_reason ?? null
})
const canSubmitCustom = computed(() => {
  const custom = customTraining.value
  if (!custom || submitting.value || customCooldownRemaining.value > 0) return false
  const trimmed = direction.value.trim()
  if (!trimmed || trimmed.length > custom.direction_max_length) return false
  if (customOperation.value === 'advance' && !selectedTargetSkill.value) return false
  return operationConfig.value?.can_start === true
})
const customSubmitLabel = computed(() => {
  if (submitting.value) {
    return mentorRequiresQueue.value ? '正在加入候场…' : '正去请教师傅…'
  }
  if (customTraining.value?.queue.is_waiting) return '已在导师候场中'
  return mentorRequiresQueue.value ? '加入导师候场队列' : '请导师指点'
})
const pendingCustomWillQueue = computed(() => (
  pendingConfirm.value?.kind === 'custom' && pendingConfirm.value.joinQueue
))
const confirmTitle = computed(() => {
  if (pendingConfirm.value?.kind === 'classic') return '跟导师学这门招式？'
  if (pendingConfirm.value?.kind === 'retry') return '按上回练法直接补课？'
  return pendingCustomWillQueue.value ? '加入导师候场队列？' : '请导师开一回小灶？'
})
const confirmLabel = computed(() => {
  if (pendingConfirm.value?.kind === 'retry') return '直接补课'
  return pendingCustomWillQueue.value ? '加入候场' : '就这么练'
})
const confirmMessage = computed(() => {
  const pending = pendingConfirm.value
  if (!pending) return ''
  if (pending.kind === 'classic') {
    return `交 ${pending.skill.gold_cost} 金币作束脩，跟着旧卷练「${pending.skill.name}」。\n会占一张传艺席，练上 ${catalog.value?.usage_duration_minutes ?? 60} 分钟。`
  }
  if (pending.kind === 'retry') {
    const retry = retryOpportunity.value
    if (!retry) return ''
    const operationLabel = retry.operation === 'advance' && retry.target_skill
      ? `把「${retry.target_skill.name}」按上回练法再磨一层`
      : '按上回练法另学一门本事'
    return `${operationLabel}\n原练法：${retry.direction ?? ''}\n本次不排队、不再等待课堂时长；开始时会检查并扣除 ${retry.gold_cost ?? 0} 金币和 ${retry.stamina_cost ?? 0} 精力。若导师仍没授成，会原样退回并继续保留补课；授艺成功后才会用掉补课并开始冷却。`
  }
  const custom = customTraining.value
  if (!custom) return ''
  const operationLabel = pending.operation === 'learn'
    ? '另学一门本事'
    : `把「${pending.targetSkill?.name ?? '所选旧招'}」再磨一层`
  const goldCost = custom.operation_config[pending.operation].gold_cost
  if (mentorRequiresQueue.value) {
    return `${operationLabel}\n你想练：${pending.direction}\n导师当前满席或已有其他人候场。确认后会先预扣 ${goldCost} 金币和 ${custom.stamina_cost} 精力，再把你排到候场队尾；候场期间不会恢复，也不能进行其他行动。入席前可以取消并原样退回，前一位离开后会自动入席，届时才开始 ${custom.usage_duration_minutes} 分钟的指点。`
  }
  return `${operationLabel}\n你想练：${pending.direction}\n这回要交 ${goldCost} 金币、花 ${custom.stamina_cost} 精力，练上 ${custom.usage_duration_minutes} 分钟。收功后导师要歇 ${custom.cooldown_hours} 小时；若路子不合身，他会按职业与特质替你收一收。`
})

async function loadCatalog() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await store.fetchSkillSchoolCatalog(props.building.id)
    if (res.code === 200 && res.data) {
      catalog.value = res.data as SkillSchoolCatalog
      const seconds = Math.max(0, Number(catalog.value.custom_training?.cooldown_remaining_seconds ?? 0))
      cooldownDeadlineMs.value = seconds > 0 ? Date.now() + seconds * 1000 : 0
      cooldownReloaded.value = false
      if (catalog.value.custom_training?.retry_opportunity) {
        mode.value = 'custom'
      }
      if (customOperation.value === 'advance' && targetSkillId.value == null) {
        targetSkillId.value = catalog.value.custom_training?.skills[0]?.id ?? null
      }
    } else {
      loadError.value = res.message || '没能取来阁中的卷册'
    }
  } catch (e: any) {
    loadError.value = e?.response?.data?.message || '没能取来阁中的卷册'
  } finally {
    loading.value = false
  }
}

function openClassicConfirm(skill: SkillSchoolOffer) {
  if (!skill.can_learn || submitting.value) return
  pendingConfirm.value = { kind: 'classic', skill }
}

function openCustomConfirm() {
  if (!canSubmitCustom.value) return
  pendingConfirm.value = {
    kind: 'custom',
    operation: customOperation.value,
    direction: direction.value.trim(),
    targetSkill: selectedTargetSkill.value,
    joinQueue: mentorRequiresQueue.value,
  }
}

function openRetryConfirm() {
  if (!retryOpportunity.value?.can_retry || submitting.value) return
  pendingConfirm.value = { kind: 'retry' }
}

async function confirmStart() {
  const pending = pendingConfirm.value
  const adv = catalog.value?.adventurer
  if (!pending || !adv || submitting.value) return

  submitting.value = true
  try {
    const res = pending.kind === 'retry'
      ? await store.retrySkillSchoolTraining(props.building.id)
      : await store.startBuildingUsage(
          props.building.id,
          adv.id,
          pending.kind === 'classic'
            ? { skill_code: pending.skill.code }
            : {
                operation: pending.operation,
                direction: pending.direction,
                ...(pending.joinQueue ? { join_queue: true } : {}),
                ...(pending.operation === 'advance' && pending.targetSkill
                  ? { target_skill_id: pending.targetSkill.id }
                  : {}),
              },
        )
    if (res.code === 200) {
      const successText = pending.kind === 'retry'
        ? '导师已经按上回练法直接开始补课'
        : pending.kind === 'classic'
        ? `导师已经带你照着旧卷练起「${pending.skill.name}」`
        : (res.data?.queued
            ? `已排到导师候场第 ${res.data.queue_position} 位`
            : pending.operation === 'learn'
            ? '导师已经接下这回指点'
            : `导师已经带你磨起「${pending.targetSkill?.name}」`)
      message.success(successText)
      pendingConfirm.value = null
      if (pending.kind === 'custom') {
        direction.value = ''
      }
      await store.fetchAdventurers()
      await loadCatalog()
      emit('refreshed')
    } else {
      message.error(res.message || '这会儿没能开练，请稍后再来')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '这会儿没能开练，请稍后再来')
  } finally {
    submitting.value = false
  }
}

async function cancelQueue() {
  const adv = catalog.value?.adventurer
  if (!adv || cancellingQueue.value) return
  cancellingQueue.value = true
  try {
    const res = await store.cancelSkillSchoolQueue(props.building.id, adv.id)
    if (res.code === 200) {
      message.success(res.message || '已取消候场')
      await store.fetchAdventurers()
      await loadCatalog()
      emit('refreshed')
    } else {
      message.error(res.message || '没能取消候场，请稍后再试')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '没能取消候场，请稍后再试')
  } finally {
    cancellingQueue.value = false
  }
}

watch(customOperation, operation => {
  if (operation === 'advance' && targetSkillId.value == null) {
    targetSkillId.value = customTraining.value?.skills[0]?.id ?? null
  }
})

watch(customCooldownRemaining, remaining => {
  if (remaining === 0 && cooldownDeadlineMs.value > 0 && !cooldownReloaded.value) {
    cooldownReloaded.value = true
    cooldownDeadlineMs.value = 0
    void loadCatalog()
  }
})

onMounted(loadCatalog)
</script>

<style scoped>
.skill-school-panel {
  margin-bottom: 16px;
}
.skill-school-header,
.skill-school-card-head,
.skill-school-card-foot,
.custom-submit-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.skill-school-header {
  justify-content: space-between;
  gap: 12px;
}
.skill-school-wallet,
.skill-school-hint,
.skill-desc,
.char-count {
  color: var(--color-text-secondary, #8b949e);
}
.skill-school-wallet {
  font-size: 0.9rem;
}
.skill-school-hint {
  margin: 10px 0 12px;
  font-size: 0.85rem;
  line-height: 1.6;
}
.mode-tabs {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 12px;
}
.mode-tab,
.operation-card {
  border: 1px solid var(--color-border, #30363d);
  background: var(--color-surface, #161b22);
  color: var(--color-text-secondary, #8b949e);
  cursor: pointer;
}
.mode-tab {
  padding: 9px 12px;
  border-radius: 8px;
}
.mode-tab.active,
.operation-card.active {
  border-color: var(--color-warning, #d29922);
  color: var(--color-text, #e6edf3);
  background: rgba(210, 153, 34, 0.12);
}
.skill-school-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.skill-school-card {
  padding: 12px;
  border: 1px solid var(--color-border, #30363d);
  border-radius: 8px;
  background: var(--color-surface, #161b22);
}
.skill-school-card.learned,
.skill-school-card.disabled:not(.learned) {
  opacity: 0.78;
}
.skill-school-card-head {
  margin-bottom: 6px;
}
.skill-name,
.field-label {
  color: var(--color-text, #e6edf3);
  font-weight: 600;
}
.skill-badge {
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 0.78rem;
}
.learned-badge {
  color: #3fb950;
  background: rgba(46, 160, 67, 0.15);
}
.price-badge {
  color: #d29922;
  background: rgba(210, 153, 34, 0.15);
}
.skill-desc {
  margin-bottom: 10px;
  font-size: 0.85rem;
  line-height: 1.5;
}
.learned-text {
  color: #3fb950;
  font-size: 0.85rem;
}
.reject-reason {
  color: #f85149;
  font-size: 0.8rem;
}
.error-state {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
}
.training-facts {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.training-facts span,
.cooldown-notice {
  padding: 6px 9px;
  border-radius: 7px;
  font-size: 0.8rem;
}
.queue-notice {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid rgba(210, 153, 34, 0.35);
  border-radius: 8px;
  color: var(--color-text, #e6edf3);
  background: rgba(210, 153, 34, 0.12);
}
.queued-request {
  padding: 12px;
  border: 1px solid var(--color-border, #30363d);
  border-radius: 8px;
  color: var(--color-text, #e6edf3);
}
.queued-direction { white-space: pre-wrap; overflow-wrap: anywhere; }
.queued-request small { color: var(--color-text-secondary, #8b949e); }
.queue-notice div {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.queue-notice span {
  color: var(--color-text-secondary, #8b949e);
  font-size: 0.8rem;
}
.retry-opportunity {
  margin-bottom: 14px;
  padding: 14px;
  border: 1px solid rgba(63, 185, 80, 0.45);
  border-radius: 10px;
  background: rgba(46, 160, 67, 0.1);
}
.retry-opportunity-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.retry-opportunity-head > div,
.retry-details dd {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.retry-opportunity-head span,
.retry-details dt,
.retry-details dd span {
  color: var(--color-text-secondary, #8b949e);
  font-size: 0.8rem;
}
.retry-badge {
  flex: 0 0 auto;
  color: #3fb950 !important;
  background: rgba(46, 160, 67, 0.18);
}
.retry-details {
  display: grid;
  gap: 9px;
  margin: 0 0 14px;
}
.retry-details div {
  display: grid;
  grid-template-columns: 76px minmax(0, 1fr);
  gap: 10px;
}
.retry-details dt,
.retry-details dd {
  margin: 0;
}
.retry-details dd {
  color: var(--color-text, #e6edf3);
  line-height: 1.45;
}
.training-facts span {
  color: var(--color-text-secondary, #8b949e);
  background: rgba(139, 148, 158, 0.1);
}
.cooldown-notice {
  margin-bottom: 12px;
  color: #d29922;
  background: rgba(210, 153, 34, 0.12);
}
.operation-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}
.operation-card {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 12px;
  border-radius: 9px;
  text-align: left;
}
.operation-card span {
  color: #d29922;
  font-size: 0.85rem;
}
.field-label {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin-bottom: 14px;
  font-size: 0.86rem;
}
.skill-select,
.direction-input {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--color-border, #30363d);
  border-radius: 8px;
  background: var(--color-surface, #0d1117);
  color: var(--color-text, #e6edf3);
}
.skill-select {
  min-height: 40px;
  padding: 8px 10px;
}
.direction-input {
  padding: 10px;
  resize: vertical;
  line-height: 1.5;
}
.char-count {
  align-self: flex-end;
  font-size: 0.76rem;
  font-weight: 400;
}
@media (max-width: 560px) {
  .operation-grid {
    grid-template-columns: 1fr;
  }
}
</style>
