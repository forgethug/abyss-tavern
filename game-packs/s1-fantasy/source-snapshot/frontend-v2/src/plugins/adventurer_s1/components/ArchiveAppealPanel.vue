<template>
  <div class="archive-appeal-panel">
    <div class="archived-icon">📦</div>
    <div class="archived-title">冒险者已封存清退</div>
    <p class="archived-desc">
      你的冒险者「{{ adventurer.name }}」档案已完整保留，但已清出酒馆编制。
      如需恢复，请提交解封申请并说明理由；管理员审核通过后将进入抽取队列，抽中后即可解封原冒险者。
    </p>
    <button type="button" class="btn-secondary btn-sm" @click="emit('view-detail')">查看档案</button>

    <!-- 审核中 -->
    <div v-if="store.myArchiveAppeal?.status === 'pending'" class="appeal-status-box">
      <div class="appeal-status-title">解封申请审核中</div>
      <p class="appeal-status-desc">已提交理由，请等待管理员审核。审核通过后将进入预约抽取队列。</p>
      <div class="appeal-reason-preview">
        <span class="label">申请理由</span>
        <p>{{ store.myArchiveAppeal.reason }}</p>
      </div>
    </div>

    <!-- 已拒绝，可重新申请 -->
    <div v-else-if="store.myArchiveAppeal?.status === 'rejected'" class="appeal-status-box rejected">
      <div class="appeal-status-title">解封申请未通过</div>
      <p v-if="store.myArchiveAppeal.reject_reason" class="appeal-reject-reason">
        拒绝原因：{{ store.myArchiveAppeal.reject_reason }}
      </p>
      <button type="button" class="btn-primary btn-sm" @click="showForm = true">重新申请解封</button>
    </div>

    <!-- 已通过：排队中（与预约队列共用临时提升） -->
    <div v-else-if="inQueue" class="appeal-status-box approved">
      <div class="appeal-status-title">已进入解封抽取队列</div>
      <p class="appeal-status-desc">
        管理员已通过你的解封申请。请耐心等待抽取；也可临时提高优先级（6 小时有效）以提高被抽中概率。
      </p>
      <div v-if="store.myReservation?.priority_boost_active" class="res-boost-active">
        已临时提高优先级，效果持续 6 小时（至 {{ boostUntilText }}）
      </div>
      <div class="queue-actions">
        <button
          v-if="store.myReservation?.can_boost_priority"
          type="button"
          class="btn-primary btn-sm"
          :disabled="boosting"
          @click="doBoost"
        >{{ boosting ? '处理中…' : '临时提高优先级（6小时）' }}</button>
        <button
          type="button"
          class="btn-secondary btn-sm"
          :disabled="cancelling"
          @click="doCancelQueue"
        >{{ cancelling ? '取消中…' : '取消排队' }}</button>
      </div>
    </div>

    <!-- 提交表单 -->
    <div v-else-if="showForm || !store.myArchiveAppeal" class="appeal-form">
      <label class="form-label" for="appeal-reason">解封理由（至少 10 字）</label>
      <textarea
        id="appeal-reason"
        v-model="reason"
        class="advs1-input appeal-textarea"
        rows="4"
        maxlength="500"
        placeholder="请说明申请解封的原因，例如误封、已改正行为等…"
      />
      <div class="form-foot">
        <span class="char-count">{{ reason.trim().length }} / 500</span>
        <button
          type="button"
          class="btn-primary btn-sm"
          :disabled="submitting || reason.trim().length < 10"
          @click="doSubmit"
        >{{ submitting ? '提交中…' : '提交解封申请' }}</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { useAdventurerS1Store, type AdventurerData } from '../stores/adventurerS1'

const props = defineProps<{ adventurer: Pick<AdventurerData, 'id' | 'name'> }>()
const emit = defineEmits<{ 'view-detail': [] }>()

const store = useAdventurerS1Store()
const message = useMessage()

const reason = ref('')
const showForm = ref(false)
const submitting = ref(false)
const boosting = ref(false)
const cancelling = ref(false)

const inQueue = computed(() =>
  store.myArchiveAppeal?.status === 'approved'
  && !!store.myReservation
  && store.myReservation.status === 'pending'
  && store.myReservation.is_unarchive_restore,
)

const boostUntilText = computed(() => {
  const until = store.myReservation?.priority_boost_until
  if (!until) return ''
  try {
    return new Date(until).toLocaleString()
  } catch {
    return until
  }
})

async function loadStatus() {
  await store.fetchMyArchiveAppeal()
  if (store.myArchiveAppeal?.status === 'approved') {
    await store.fetchReservationStatus()
  }
}

async function doSubmit() {
  if (submitting.value) return
  const text = reason.value.trim()
  if (text.length < 10) {
    message.warning('请填写至少 10 字的解封理由')
    return
  }
  submitting.value = true
  try {
    const res = await store.submitArchiveAppeal(props.adventurer.id, text)
    if (res.code === 200 || res.code === 201) {
      message.success('解封申请已提交，请等待管理员审核')
      showForm.value = false
      reason.value = ''
    } else {
      message.error(res.message || '提交失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message.error(err?.response?.data?.message || '提交失败')
  } finally {
    submitting.value = false
  }
}

async function doBoost() {
  if (!confirm('确定临时提高优先级吗？效果持续 6 小时，期间无法重复使用。')) return
  boosting.value = true
  try {
    const res = await store.boostReservationPriority()
    if (res.code === 200) {
      message.success(res.data?.priority_boost_active
        ? '已临时提高优先级，效果持续 6 小时'
        : '预约状态已更新，请以当前排队状态为准')
    } else {
      message.error(res.message || '操作失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message.error(err?.response?.data?.message || '操作失败')
  } finally {
    boosting.value = false
  }
}

async function doCancelQueue() {
  if (!confirm('确定取消解封排队吗？取消后需重新提交解封申请。')) return
  cancelling.value = true
  try {
    const res = await store.cancelReservation()
    if (res.code === 200) {
      message.success('已取消排队')
      store.myArchiveAppeal = null
      await loadStatus()
    } else {
      message.error(res.message || '取消失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message.error(err?.response?.data?.message || '取消失败')
  } finally {
    cancelling.value = false
  }
}

onMounted(() => {
  void loadStatus()
})
</script>

<style scoped>
.archive-appeal-panel {
  text-align: center;
  padding: 1.5rem 1rem;
  border: 1px dashed var(--color-border, #dee2e6);
  border-radius: 12px;
  background: var(--color-surface, #fff);
}

.archived-icon {
  font-size: 2rem;
  margin-bottom: 0.5rem;
}

.archived-title {
  font-size: 1.1rem;
  font-weight: 600;
  margin-bottom: 0.5rem;
  color: var(--color-text, #212529);
}

.archived-desc {
  color: var(--color-text-muted, #6c757d);
  font-size: 0.9rem;
  line-height: 1.5;
  margin: 0 0 1rem;
  max-width: 36rem;
  margin-left: auto;
  margin-right: auto;
}

.appeal-status-box {
  margin-top: 1rem;
  padding: 1rem;
  border-radius: 8px;
  background: var(--color-bg, #f8f9fa);
  text-align: left;
}

.appeal-status-box.rejected {
  border: 1px solid #f5c2c7;
  background: #fff5f5;
}

.appeal-status-box.approved {
  border: 1px solid #b6d4fe;
  background: #f0f7ff;
}

.appeal-status-title {
  font-weight: 600;
  margin-bottom: 0.35rem;
}

.appeal-status-desc {
  font-size: 0.875rem;
  color: var(--color-text-muted, #6c757d);
  margin: 0 0 0.75rem;
}

.appeal-reason-preview .label {
  display: block;
  font-size: 0.75rem;
  color: var(--color-text-muted, #6c757d);
  margin-bottom: 0.25rem;
}

.appeal-reason-preview p {
  margin: 0;
  white-space: pre-wrap;
  font-size: 0.9rem;
}

.appeal-reject-reason {
  font-size: 0.875rem;
  color: #842029;
  margin: 0 0 0.75rem;
}

.appeal-form {
  margin-top: 1rem;
  text-align: left;
  max-width: 28rem;
  margin-left: auto;
  margin-right: auto;
}

.form-label {
  display: block;
  font-size: 0.875rem;
  margin-bottom: 0.35rem;
}

.appeal-textarea {
  width: 100%;
  min-height: 6rem;
  resize: vertical;
}

.form-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 0.5rem;
  gap: 0.5rem;
}

.char-count {
  font-size: 0.75rem;
  color: var(--color-text-muted, #6c757d);
}

.queue-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.res-boost-active {
  font-size: 0.875rem;
  color: #0a58ca;
  margin-bottom: 0.75rem;
}
</style>
