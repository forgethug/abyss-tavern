<template>
  <Teleport to="body">
    <div v-if="visible" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="close">
      <div class="modal-content task-admin-modal s1-mobile-dialog">
        <div class="modal-header s1-mobile-dialog-header">
          <h4>任务栏管理</h4>
          <button type="button" class="modal-close" @click="close">×</button>
        </div>
        <div class="modal-body s1-mobile-dialog-body">
          <p class="admin-hint">
            可在此启用/停用任务、调整奖励与名额。「发布新版本」会停用当前任务并生成带新 ID 的独立任务（如 gathering_contract_1h__r1），名额从零开始，正式服请在部署后于此操作，无需手改数据库。
          </p>
          <div v-if="loading" class="empty-text">加载中…</div>
          <div v-else class="task-admin-layout">
            <div class="task-admin-list">
              <button
                v-for="task in tasks"
                :key="task.id"
                type="button"
                class="task-admin-item"
                :class="{ active: selectedId === task.id, ended: task.is_ended, disabled: !task.enabled }"
                @click="selectTask(task)"
              >
                <span class="item-icon">{{ task.template.icon ?? '📜' }}</span>
                <span class="item-main">
                  <span class="item-name">{{ task.template.name }}</span>
                  <span class="item-meta">
                    {{ completionTypeLabel(task.completion_type) }}
                    <template v-if="task.is_edition"> · {{ task.id }}</template>
                  </span>
                </span>
                <span class="item-status">
                  <template v-if="!task.enabled">已停用</template>
                  <template v-else-if="task.is_ended">已结束</template>
                  <template v-else-if="task.max_completions != null">
                    余 {{ task.completion_remaining ?? 0 }}/{{ task.max_completions }}
                  </template>
                  <template v-else>开放</template>
                </span>
              </button>
            </div>

            <div v-if="editForm" class="task-admin-form">
              <div class="form-row">
                <label>
                  <input v-model="editForm.enabled" type="checkbox" />
                  对玩家展示
                </label>
              </div>
              <div class="form-row">
                <label class="field-label">任务名称</label>
                <input v-model="editForm.name" type="text" class="advs1-input" maxlength="40" />
              </div>
              <div class="form-row">
                <label class="field-label">描述</label>
                <textarea v-model="editForm.description" class="advs1-input" rows="2" maxlength="200" />
              </div>
              <div class="form-row two-col">
                <div>
                  <label class="field-label">全服名额</label>
                  <input v-model.number="editForm.max_completions" type="number" min="1" class="advs1-input" />
                </div>
                <div>
                  <label class="field-label">金币奖励</label>
                  <input v-model.number="editForm.reward_gold" type="number" min="0" class="advs1-input" />
                </div>
              </div>
              <div v-if="selectedTask?.completion_type === 'expedition'" class="form-row two-col">
                <div>
                  <label class="field-label">目标副本</label>
                  <input v-model="editForm.required_dungeon" type="text" class="advs1-input" placeholder="留空=任意副本" />
                </div>
                <div class="checkbox-row">
                  <label>
                    <input v-model="editForm.no_damage" type="checkbox" />
                    须无伤通关
                  </label>
                </div>
              </div>
              <div v-if="selectedTask?.completion_type === 'building_help' || selectedTask?.completion_type === 'gathering'" class="form-row">
                <label class="field-label">所需时长（分钟）</label>
                <input v-model.number="editForm.duration_minutes" type="number" min="1" class="advs1-input" />
              </div>
              <div v-if="selectedTask?.completion_type === 'work'" class="form-row">
                <label class="field-label">所需打工次数</label>
                <input v-model.number="editForm.required_sessions" type="number" min="1" class="advs1-input" />
              </div>
              <div class="form-row">
                <label>
                  <input v-model="editForm.repeatable" type="checkbox" />
                  同一冒险者可重复完成
                </label>
              </div>
              <div v-if="selectedTask" class="stats-box">
                <div>任务 ID：{{ selectedTask.id }}</div>
                <div v-if="selectedTask.source_template_id">来源模板：{{ selectedTask.source_template_id }}</div>
                <div>已完成：{{ selectedTask.completion_count ?? 0 }}</div>
                <div>占用名额：{{ selectedTask.occupied_count ?? 0 }}</div>
              </div>
              <div class="form-actions">
                <button type="button" class="advs1-submit" :disabled="saving" @click="save">
                  {{ saving ? '保存中…' : '保存修改' }}
                </button>
                <button type="button" class="btn-secondary" :disabled="reenabling" @click="reenable">
                  {{ reenabling ? '处理中…' : '发布新版本' }}
                </button>
              </div>
            </div>
            <div v-else class="task-admin-empty">请选择左侧任务</div>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { useAdventurerS1Store, type TaskAdminItem } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [value: boolean]; saved: [] }>()

const store = useAdventurerS1Store()
const message = useGlobalMessage()

const loading = ref(false)
const saving = ref(false)
const reenabling = ref(false)
const tasks = ref<TaskAdminItem[]>([])
const selectedId = ref<string | null>(null)
const selectedTask = ref<TaskAdminItem | null>(null)

interface EditForm {
  enabled: boolean
  name: string
  description: string
  max_completions: number
  reward_gold: number
  duration_minutes: number
  required_sessions: number
  required_dungeon: string
  no_damage: boolean
  repeatable: boolean
}

const editForm = ref<EditForm | null>(null)

function completionTypeLabel(type?: string) {
  return ({
    expedition: '探险',
    building_help: '帮工',
    gathering: '采集',
    work: '打工',
    contribution: '贡献',
    timer: '计时',
  } as Record<string, string>)[type ?? ''] ?? type ?? '任务'
}

function buildEditForm(task: TaskAdminItem): EditForm {
  return {
    enabled: task.enabled,
    name: task.template.name ?? '',
    description: task.template.description ?? '',
    max_completions: task.template.max_completions ?? task.max_completions ?? 1,
    reward_gold: task.template.rewards?.gold ?? 0,
    duration_minutes: task.template.duration_minutes ?? 0,
    required_sessions: task.template.required_sessions ?? 1,
    required_dungeon: task.template.required_dungeon ?? '',
    no_damage: !!task.template.no_damage,
    repeatable: !!task.template.repeatable,
  }
}

function selectTask(task: TaskAdminItem) {
  selectedId.value = task.id
  selectedTask.value = task
  editForm.value = buildEditForm(task)
}

async function loadTasks() {
  loading.value = true
  try {
    const res = await store.adminListTasks()
    if (res.code !== 200) {
      message.error(res.message || '加载任务配置失败')
      return
    }
    tasks.value = res.data ?? []
    if (tasks.value.length > 0) {
      const current = tasks.value.find(t => t.id === selectedId.value) ?? tasks.value[0]
      selectTask(current)
    }
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!selectedId.value || !editForm.value) return
  saving.value = true
  try {
    const form = editForm.value
    const res = await store.adminUpdateTask(selectedId.value, {
      enabled: form.enabled,
      overrides: {
        name: form.name.trim(),
        description: form.description.trim(),
        max_completions: form.max_completions,
        duration_minutes: form.duration_minutes,
        required_sessions: form.required_sessions,
        required_dungeon: form.required_dungeon.trim() || undefined,
        no_damage: form.no_damage,
        repeatable: form.repeatable,
        rewards: { gold: form.reward_gold },
      },
    })
    if (res.code === 200) {
      message.success('任务配置已保存')
      await loadTasks()
      emit('saved')
    } else {
      message.error(res.message || '保存失败')
    }
  } finally {
    saving.value = false
  }
}

async function reenable() {
  if (!selectedId.value || !editForm.value) return
  reenabling.value = true
  try {
    const res = await store.adminReenableTask(selectedId.value, {
      max_completions: editForm.value.max_completions,
    })
    if (res.code === 200) {
      const newId = res.data?.id
      message.success(newId ? `已发布新版本：${newId}` : '已发布新版本')
      await loadTasks()
      if (newId) {
        const created = tasks.value.find(t => t.id === newId)
        if (created) selectTask(created)
      }
      emit('saved')
    } else {
      message.error(res.message || '重新启用失败')
    }
  } finally {
    reenabling.value = false
  }
}

function close() {
  emit('update:visible', false)
}

watch(() => props.visible, (open) => {
  if (open) loadTasks()
})
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1100;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.45);
  padding: 16px;
}

.modal-content {
  width: min(920px, 100%);
  max-height: min(88vh, 900px);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 12px;
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  border-bottom: 1px solid var(--color-border);
}

.modal-header h4 {
  margin: 0;
  font-size: 1rem;
}

.modal-close {
  border: none;
  background: transparent;
  color: var(--color-text);
  font-size: 1.4rem;
  line-height: 1;
  cursor: pointer;
}

.modal-body {
  padding: 14px 16px 16px;
  overflow-y: auto;
}

.admin-hint {
  margin: 0 0 12px;
  font-size: 0.82em;
  color: var(--color-text-secondary, var(--color-text));
  opacity: 0.85;
}

.task-admin-layout {
  display: grid;
  grid-template-columns: minmax(220px, 280px) 1fr;
  gap: 14px;
  min-height: 360px;
}

.task-admin-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 420px;
  overflow-y: auto;
}

.task-admin-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface, var(--color-bg));
  color: var(--color-text);
  cursor: pointer;
  text-align: left;
}

.task-admin-item.active {
  border-color: var(--color-primary);
}

.task-admin-item.disabled,
.task-admin-item.ended {
  opacity: 0.72;
}

.item-icon {
  font-size: 1.2em;
}

.item-main {
  flex: 1;
  min-width: 0;
}

.item-name {
  display: block;
  font-weight: 600;
  font-size: 0.9em;
}

.item-meta {
  display: block;
  font-size: 0.75em;
  opacity: 0.75;
}

.item-status {
  font-size: 0.72em;
  white-space: nowrap;
}

.task-admin-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.form-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.form-row.two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.field-label {
  font-size: 0.82em;
  opacity: 0.85;
}

.checkbox-row {
  display: flex;
  align-items: flex-end;
  padding-bottom: 6px;
}

.stats-box {
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px dashed var(--color-border);
  font-size: 0.82em;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.form-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 4px;
}

.btn-secondary {
  padding: 8px 14px;
  border-radius: 8px;
  border: 1px solid var(--color-border);
  background: var(--color-surface, var(--color-bg));
  color: var(--color-text);
  cursor: pointer;
}

.btn-secondary:disabled,
.advs1-submit:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.task-admin-empty,
.empty-text {
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text);
  opacity: 0.7;
  min-height: 200px;
}

@media (max-width: 720px) {
  .task-admin-layout {
    grid-template-columns: 1fr;
  }
}
</style>
