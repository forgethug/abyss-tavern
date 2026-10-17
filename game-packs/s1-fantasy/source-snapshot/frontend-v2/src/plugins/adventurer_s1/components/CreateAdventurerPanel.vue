<template>
  <section class="advs1-card">
    <div v-if="loading" class="advs1-state">加载中…</div>

    <!-- 已有预约：显示等待状态 + 操作按钮 -->
    <div v-else-if="store.myReservation && !editing" class="reservation-status">
      <div class="res-icon">📋</div>
      <div class="res-title">预约已提交，等待管理员抽取</div>
      <div class="res-detail">
        <span>冒险者：<strong>{{ store.myReservation.name }}</strong></span>
        <span>性别：{{ store.myReservation.gender }}</span>
        <span>性格：{{ store.myReservation.personality }}</span>
      </div>
      <div class="res-hint">预约创建后，请耐心等待。管理员抽取时你将自动获得冒险者。</div>
      <div v-if="store.myReservation.priority_boost_active" class="res-boost-active">
        已临时提高优先级，效果持续 6 小时（至 {{ boostUntilText }}）
      </div>
      <div class="res-actions">
        <button
          v-if="store.myReservation.can_boost_priority"
          type="button"
          class="advs1-btn-boost"
          :disabled="boosting"
          @click="boostPriority"
        >
          {{ boosting ? '处理中…' : '临时提高优先级（6小时）' }}
        </button>
        <button type="button" class="advs1-btn-outline" @click="startEdit">修改预约</button>
        <button type="button" class="advs1-btn-danger" :disabled="cancelling" @click="cancelRes">
          {{ cancelling ? '取消中…' : '取消预约' }}
        </button>
      </div>
      <ReservationAppearanceHistory
        v-if="!store.myReservation.is_unarchive_restore"
        :key="`${store.squadSessionUserId}:${store.realmViewCode}:${store.myReservation.id}`"
      />
    </div>

    <template v-else-if="!store.myReservation || editing">
      <p class="panel-tip">
        选择 1 个初始技能与 2 件初始装备，提交预约。管理员将定期从预约队列中抽取冒险者。
      </p>
      <p class="panel-tip">预约与角色属于当前酒馆；其他酒馆的角色和进度独立保存。</p>

      <div class="form-grid">
        <div class="form-group">
          <label>冒险者姓名 <span class="req">*</span></label>
          <input v-model="form.name" type="text" :maxlength="nameMaxLength" placeholder="请输入姓名" class="advs1-input" />
          <small :class="['hint', { 'hint-error': nameError }]">{{ nameError || `只能中英文，最多 ${nameMaxLength} 个字；不可与世界情报实体或其别名重名` }}</small>
        </div>
        <div class="form-group">
          <label>性别 <span class="req">*</span></label>
          <select v-model="form.gender" class="advs1-input">
            <option v-for="g in genders" :key="g" :value="g">{{ g }}</option>
          </select>
        </div>
        <div class="form-group">
          <label>性格 <span class="req">*</span></label>
          <select v-model="form.personality" class="advs1-input">
            <option v-for="p in personalities" :key="p" :value="p">{{ p }}</option>
          </select>
        </div>
      </div>

      <div class="select-section">
        <h4 class="section-h">选择初始技能 <span class="req">*</span></h4>
        <div class="card-grid">
          <button
            v-for="s in skills"
            :key="s.id"
            type="button"
            :class="['choice-card', { active: form.skillId === s.id }]"
            @click="form.skillId = s.id"
          >
            <div class="choice-name">{{ s.name }}</div>
            <div class="choice-desc">{{ s.description }}</div>
          </button>
        </div>
      </div>

      <div class="select-section">
        <h4 class="section-h">选择初始装备（2件） <span class="req">*</span></h4>
        <div class="item-type-tabs">
          <button
            v-for="tab in itemTypeTabs"
            :key="tab.key"
            type="button"
            :class="['type-tab', { active: itemTab === tab.key }]"
            @click="itemTab = tab.key"
          >{{ tab.label }}</button>
        </div>
        <div class="card-grid">
          <button
            v-for="it in filteredItems"
            :key="it.id"
            type="button"
            :class="['choice-card', { active: form.itemIds.includes(it.id) }]"
            @click="toggleItem(it.id)"
          >
            <div class="choice-name">
              {{ it.name }}
              <span class="type-tag">{{ itemTypeName(it.type) }}</span>
              <span class="rarity-pill" :style="{ color: rarityColor(it.rarity), borderColor: rarityColor(it.rarity) }">
                {{ rarityLabel(it.rarity) }}
              </span>
            </div>
            <div class="choice-desc">{{ it.description }}</div>
          </button>
        </div>
        <small class="hint">已选 {{ form.itemIds.length }}/2</small>
      </div>

      <div class="submit-bar">
        <button v-if="editing" type="button" class="advs1-btn-outline" :disabled="submitting" @click="cancelEdit" style="margin-right: 10px;">取消修改</button>
        <button type="button" class="advs1-submit" :disabled="!canSubmit" @click="submit">
          {{ submitting ? '提交中…' : (editing ? '保存修改' : '提交预约') }}
        </button>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { useAdventurerS1Store, type SkillTemplate, type StarterItem } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import ReservationAppearanceHistory from './ReservationAppearanceHistory.vue'
import { rarityColor, rarityLabel, itemTypeLabel, STARTER_ITEM_TYPE_ORDER } from '../utils'

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const emit = defineEmits<{ reserved: [] }>()

const loading = ref(true)
const submitting = ref(false)
const cancelling = ref(false)
const boosting = ref(false)
const editing = ref(false)
const itemTab = ref<string>('all')

const form = reactive({
  name: '',
  gender: '',
  personality: '',
  skillId: null as number | null,
  itemIds: [] as number[],
})

const skills = computed<SkillTemplate[]>(() => store.options?.skills ?? [])
const allItems = computed<StarterItem[]>(() => store.options?.items ?? [])
const genders = computed<string[]>(() => store.options?.genders ?? [])
const personalities = computed<string[]>(() => store.options?.personalities ?? [])
const nameMaxLength = computed<number>(() => store.options?.name_max_length ?? 7)
// 仅中文（CJK 基本区）与英文字母，与后端校验保持一致
const NAME_RE = /^[一-鿿a-zA-Z]+$/
const nameError = computed<string>(() => {
  const n = form.name.trim()
  if (!n) return ''
  if (!NAME_RE.test(n)) return '只能包含中文或英文字母，不能有数字、空格或符号'
  return ''
})

const itemTypeTabs = computed(() => {
  const types = new Set(allItems.value.map(i => i.type))
  const tabs = [{ key: 'all', label: '全部' }]
  for (const t of STARTER_ITEM_TYPE_ORDER) {
    if (types.has(t)) tabs.push({ key: t, label: itemTypeLabel(t) })
  }
  for (const t of types) {
    if (!STARTER_ITEM_TYPE_ORDER.includes(t as (typeof STARTER_ITEM_TYPE_ORDER)[number])) {
      tabs.push({ key: t, label: itemTypeLabel(t) })
    }
  }
  return tabs
})

const filteredItems = computed(() => {
  if (itemTab.value === 'all') return allItems.value
  return allItems.value.filter(i => i.type === itemTab.value)
})

const boostUntilText = computed(() => {
  const until = store.myReservation?.priority_boost_until
  if (!until) return ''
  const d = new Date(until)
  if (Number.isNaN(d.getTime())) return until
  return d.toLocaleString('zh-CN', { hour12: false })
})

function itemTypeName(type: string): string {
  return itemTypeLabel(type)
}

function toggleItem(id: number) {
  const idx = form.itemIds.indexOf(id)
  if (idx >= 0) {
    form.itemIds.splice(idx, 1)
  } else if (form.itemIds.length < 2) {
    form.itemIds.push(id)
  }
}

async function startEdit() {
  const r = store.myReservation
  if (!r) return
  await store.fetchCreationOptions()
  form.name = r.name
  form.gender = r.gender
  form.personality = r.personality
  form.skillId = r.skill_template_id
  form.itemIds = [...r.item_ids]
  editing.value = true
}

function cancelEdit() {
  editing.value = false
}

async function cancelRes() {
  if (!confirm('确定要取消预约吗？取消后需要重新提交。')) return
  cancelling.value = true
  try {
    const data = await store.cancelReservation()
    if (data.code === 200) {
      message.success('预约已取消')
      try {
        await store.fetchCreationOptions()
      } catch {
        message.warning('区服信息刷新失败，请刷新页面后再提交')
      }
    } else {
      message.error(data.message || '取消失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '取消失败')
  } finally {
    cancelling.value = false
  }
}

async function boostPriority() {
  if (!confirm('确定临时提高优先级吗？效果持续 6 小时，期间无法重复使用。')) return
  boosting.value = true
  try {
    const data = await store.boostReservationPriority()
    if (data.code === 200) {
      message.success(data.data?.priority_boost_active
        ? '已临时提高优先级，效果持续 6 小时'
        : '预约状态已更新，请以当前排队状态为准')
    } else {
      message.error(data.message || '操作失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  } finally {
    boosting.value = false
  }
}

const canSubmit = computed(
  () =>
    !!form.name.trim() &&
    !nameError.value &&
    !!form.gender &&
    !!form.personality &&
    form.skillId != null &&
    form.itemIds.length === 2 &&
    !submitting.value,
)

onMounted(async () => {
  try {
    await store.fetchReservationStatus()
    if (store.myReservation) {
      loading.value = false
      return
    }
    // 创建选项含当前账号的动态区服预览，进入表单时必须重新读取。
    await store.fetchCreationOptions()
    const o = store.options
    if (o) {
      form.gender = o.genders[0] ?? ''
      form.personality = o.personalities[0] ?? ''
    }
  } catch {
    message.error('加载失败')
  } finally {
    loading.value = false
  }
})

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  try {
    const payload = {
      name: form.name.trim(),
      gender: form.gender,
      personality: form.personality,
      skill_template_id: form.skillId as number,
      item_ids: [...form.itemIds],
    }
    let data
    if (editing.value) {
      data = await store.updateReservation(payload)
      if (data.code === 200) {
        message.success('预约已更新！')
        editing.value = false
      } else {
        message.error(data.message || '修改失败')
      }
    } else {
      data = await store.submitReservation(payload)
      if (data.code === 200 || data.code === 201) {
        message.success('预约已提交，请等待管理员抽取！')
        emit('reserved')
      } else {
        message.error(data.message || '预约失败')
      }
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped lang="scss">
.advs1-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 20px;
}

.advs1-state {
  padding: 32px 0;
  text-align: center;
  color: var(--color-text-secondary);
}

.reservation-status {
  text-align: center;
  padding: 16px 0;
}

.res-icon {
  font-size: 40px;
  margin-bottom: 8px;
}

.res-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--color-primary);
  margin-bottom: 12px;
}

.res-detail {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 12px;
  font-size: 13px;
  color: var(--color-text);
  margin-bottom: 8px;
}

.res-hint {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.res-boost-active {
  margin-top: 10px;
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--color-primary) 35%, transparent);
  border-radius: 8px;
}

.res-actions {
  display: flex;
  justify-content: center;
  gap: 10px;
  margin-top: 14px;
}

.advs1-btn-outline {
  padding: 7px 20px;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;

  &:hover:not(:disabled) {
    border-color: var(--color-primary);
    background: color-mix(in srgb, var(--color-primary) 8%, transparent);
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.advs1-btn-boost {
  padding: 7px 20px;
  font-size: 13px;
  font-weight: 600;
  color: #fff;
  background: var(--color-primary);
  border: none;
  border-radius: 6px;
  cursor: pointer;
  transition: opacity 0.15s;

  &:hover:not(:disabled) {
    opacity: 0.88;
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.advs1-btn-danger {
  padding: 7px 20px;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-danger);
  background: transparent;
  border: 1px solid color-mix(in srgb, var(--color-danger) 40%, transparent);
  border-radius: 6px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;

  &:hover:not(:disabled) {
    border-color: var(--color-danger);
    background: color-mix(in srgb, var(--color-danger) 10%, transparent);
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.panel-tip {
  margin: 0 0 16px;
  font-size: 13px;
  color: var(--color-text-secondary);
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  margin-bottom: 8px;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 6px;

  label {
    font-size: 13px;
    font-weight: 600;
    color: var(--color-text);
  }

  .hint {
    font-size: 12px;
    color: var(--color-text-secondary);
  }

  .hint-error {
    color: var(--color-danger);
  }
}

.req {
  color: var(--color-danger);
}

.advs1-input {
  width: 100%;
  padding: 8px 10px;
  font-size: 14px;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  outline: none;
  transition: border-color 0.15s;

  &:focus {
    border-color: var(--color-primary);
  }
}

.select-section {
  margin-top: 20px;
}

.item-type-tabs {
  display: flex;
  gap: 6px;
  margin-bottom: 10px;
}

.type-tab {
  padding: 4px 14px;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: 14px;
  cursor: pointer;
  transition: all 0.15s;

  &:hover {
    border-color: var(--color-primary);
    color: var(--color-text);
  }

  &.active {
    background: var(--color-primary);
    border-color: var(--color-primary);
    color: #fff;
  }
}

.type-tag {
  font-size: 10px;
  font-weight: 600;
  padding: 0 5px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--color-text-secondary) 12%, transparent);
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.section-h {
  margin: 0 0 10px;
  font-size: 15px;
  color: var(--color-text);
}

.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 10px;
}

.choice-card {
  text-align: left;
  padding: 12px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;

  &:hover {
    border-color: var(--color-primary);
    background: var(--color-hover);
  }

  &.active {
    border-color: var(--color-primary);
    background: color-mix(in srgb, var(--color-primary) 14%, transparent);
    box-shadow: 0 0 0 1px var(--color-primary) inset;
  }
}

.choice-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 700;
  color: var(--color-text);
  margin-bottom: 4px;
}

.choice-desc {
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text-secondary);
}

.rarity-pill {
  font-size: 11px;
  font-weight: 600;
  padding: 0 6px;
  border: 1px solid currentColor;
  border-radius: 10px;
  line-height: 1.6;
}

.submit-bar {
  margin-top: 24px;
  display: flex;
  justify-content: flex-end;
}

.advs1-submit {
  padding: 10px 28px;
  font-size: 15px;
  font-weight: 700;
  color: #fff;
  background: var(--color-primary);
  border: none;
  border-radius: 8px;
  cursor: pointer;
  transition: opacity 0.15s;

  &:hover:not(:disabled) {
    opacity: 0.88;
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

/* ── 移动端适配 ── */
@media (max-width: 640px) {
  .advs1-card {
    padding: 14px;
  }

  .item-type-tabs {
    flex-wrap: wrap;
  }

  /* iOS Safari 表单控件字号 <16px 聚焦时会强制放大页面 */
  .advs1-input {
    font-size: 16px;
  }

  .submit-bar .advs1-submit {
    flex: 1;
  }
}
@media (max-width: 640px) {
  .form-grid, .card-grid { grid-template-columns: minmax(0, 1fr); }
  .form-grid > *, .card-grid > * { min-width: 0; }
}
</style>
