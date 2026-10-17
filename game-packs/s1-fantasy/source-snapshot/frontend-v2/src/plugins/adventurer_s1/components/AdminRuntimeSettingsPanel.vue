<template>
  <div class="runtime-settings">
    <div class="settings-toolbar">
      <div>
        <h6>S1 运行与 AI 设置</h6>
        <p>统一管理组队、发车配速、AI 停用时段和模型连接。时间均按北京时间。</p>
      </div>
      <button type="button" class="btn-secondary btn-sm" :disabled="loading" @click="loadAll">
        {{ loading ? '加载中…' : '刷新运行设置' }}
      </button>
    </div>

    <div v-if="loading" class="settings-state">加载中…</div>
    <template v-else>
      <section class="settings-card">
        <h6>组队与发车</h6>
        <div class="field-grid">
          <label><span>队伍最小人数</span><input v-model.number="form.party_min_size" type="number" min="1" max="20" /></label>
          <label><span>队伍最大人数</span><input v-model.number="form.party_max_size" type="number" min="1" max="20" /></label>
          <label><span>特殊小队倒计时（分钟）</span><input v-model.number="form.departure_minutes" type="number" min="1" max="1440" /></label>
          <label><span>同档冷却（分钟）</span><input v-model.number="form.rank_cooldown_minutes" type="number" min="0" max="10080" /></label>
          <label><span>同档 forming 上限</span><input v-model.number="form.max_forming_per_rank" type="number" min="1" max="10" /></label>
          <label><span>组队 tick（分钟）</span><input v-model.number="form.party_form_interval_minutes" type="number" min="1" max="1440" /></label>
          <label><span>自动发车 tick（分钟）</span><input v-model.number="form.expedition_dispatch_interval_minutes" type="number" min="1" max="60" /></label>
          <label><span>剧情推进 tick（分钟）</span><input v-model.number="form.expedition_progress_interval_minutes" type="number" min="1" max="60" /></label>
          <label><span>每日探险配额（0=不限）</span><input v-model.number="form.daily_expedition_quota" type="number" min="0" max="10000" /></label>
        </div>
        <p class="settings-hint">今日已用：{{ form.daily_expedition_used }} / {{ form.daily_expedition_quota || '∞' }}</p>
        <label class="toggle-row"><input v-model="form.hidden_dungeon_enabled" type="checkbox" /><strong>启用隐藏副本</strong></label>
        <p class="settings-hint">关闭后只停止新的隐藏副本触发；已有探险日志、藏品掉落和秘境收藏馆兑换不受影响。</p>
      </section>

      <div class="period-grid">
        <section class="settings-card">
          <label class="toggle-row"><input v-model="form.slow_period_enabled" type="checkbox" /><strong>启用慢时段</strong></label>
          <div class="field-grid compact" :class="{ muted: !form.slow_period_enabled }">
            <label><span>开始（整点）</span><input v-model.number="form.slow_period_start_hour" type="number" min="0" max="23" :disabled="!form.slow_period_enabled" /></label>
            <label><span>结束（整点）</span><input v-model.number="form.slow_period_end_hour" type="number" min="0" max="23" :disabled="!form.slow_period_enabled" /></label>
            <label><span>组队概率倍率</span><input v-model.number="form.slow_period_factor" type="number" min="0" max="1" step="0.05" :disabled="!form.slow_period_enabled" /></label>
          </div>
          <p class="settings-hint">降低慢时段的自动成团概率；关闭后该时间段按普通速度运行。</p>
        </section>

        <section class="settings-card">
          <label class="toggle-row"><input v-model="form.fast_period_enabled" type="checkbox" /><strong>启用快时段</strong></label>
          <div class="field-grid compact" :class="{ muted: !form.fast_period_enabled }">
            <label><span>开始（整点）</span><input v-model.number="form.fast_period_start_hour" type="number" min="0" max="23" :disabled="!form.fast_period_enabled" /></label>
            <label><span>结束（整点）</span><input v-model.number="form.fast_period_end_hour" type="number" min="0" max="23" :disabled="!form.fast_period_enabled" /></label>
            <label><span>加速倍率</span><input v-model.number="form.fast_period_factor" type="number" min="1" max="10" step="0.5" :disabled="!form.fast_period_enabled" /></label>
          </div>
          <p class="settings-hint">提高成团概率，并按倍率缩短动态发车间隔、放宽单轮发车上限；与慢时段重叠时快时段优先。</p>
        </section>
      </div>

      <section class="settings-card danger-zone">
        <h6>AI 调用控制</h6>
        <p class="settings-hint">全局总闸已放在酒馆管理台顶部，点击后立即保存；这里仅配置按时段自动停用。</p>
        <label class="toggle-row"><input v-model="form.ai_blackout_enabled" type="checkbox" /><strong>启用按时段停用 AI</strong></label>
        <div class="window-list" :class="{ muted: !form.ai_blackout_enabled }">
          <div v-for="(window, index) in form.ai_blackout_windows" :key="index" class="window-row">
            <input v-model="window.start" type="time" :disabled="!form.ai_blackout_enabled" />
            <span>至</span>
            <input v-model="window.end" type="time" :disabled="!form.ai_blackout_enabled" />
            <button type="button" class="btn-secondary btn-sm" :disabled="!form.ai_blackout_enabled" @click="removeWindow(index)">删除</button>
          </div>
          <button type="button" class="btn-secondary btn-sm add-window" :disabled="!form.ai_blackout_enabled || form.ai_blackout_windows.length >= 12" @click="addWindow">+ 添加停用时段</button>
        </div>
        <p class="settings-hint">命中停用时段时，所有新的 S1 外部 AI 请求都会被后端闸门拦截；待发小队保留，恢复后继续发车。</p>
      </section>

      <div class="save-row">
        <button type="button" class="btn-primary btn-sm" :disabled="savingSchedule" @click="saveSchedule">
          {{ savingSchedule ? '保存中…' : '保存运行设置' }}
        </button>
      </div>

      <AdminAiModelConfigPanel scope="s1" />

      <section class="settings-card">
        <div class="settings-toolbar compact-toolbar">
          <div><h6>发车诊断</h6><p>只读检查动态间隔、单轮上限与满员积压。</p></div>
          <button type="button" class="btn-secondary btn-sm" :disabled="diagnosing" @click="loadDiagnosis">{{ diagnosing ? '诊断中…' : '运行诊断' }}</button>
        </div>
        <div v-if="diagnosis" class="diagnosis-grid">
          <span>北京时间 {{ diagnosis.bj_now }}</span>
          <span>配额 {{ diagnosis.used_today }} / {{ diagnosis.quota || '∞' }}</span>
          <span>动态间隔 {{ diagnosis.required_gap_minutes ?? '—' }} 分钟</span>
          <span>单轮上限 {{ diagnosis.cap_now ?? '—' }}</span>
          <span>此刻放行 {{ diagnosis.allowance_now ?? '—' }} 支</span>
          <span>满员积压 {{ diagnosis.backlog_due ?? 0 }} 支</span>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import AdminAiModelConfigPanel from '@/components/admin/AdminAiModelConfigPanel.vue'
import api from '@/composables/useApi'
import { useGlobalMessage } from '@/composables/useGlobalMessage'

interface BlackoutWindow { start: string; end: string }

const message = useGlobalMessage()
// Keep the model panel unmounted until the first settings read completes.
const loading = ref(true)
const savingSchedule = ref(false)
const diagnosing = ref(false)
const diagnosis = ref<Record<string, any> | null>(null)

const form = reactive({
  party_min_size: 4,
  party_max_size: 6,
  departure_minutes: 30,
  rank_cooldown_minutes: 15,
  max_forming_per_rank: 2,
  slow_period_enabled: true,
  slow_period_start_hour: 2,
  slow_period_end_hour: 9,
  slow_period_factor: 0.25,
  fast_period_enabled: false,
  fast_period_start_hour: 18,
  fast_period_end_hour: 23,
  fast_period_factor: 2,
  party_form_interval_minutes: 5,
  expedition_dispatch_interval_minutes: 1,
  expedition_progress_interval_minutes: 5,
  daily_expedition_quota: 100,
  daily_expedition_used: 0,
  hidden_dungeon_enabled: true,
  ai_blackout_enabled: false,
  ai_blackout_windows: [] as BlackoutWindow[],
})

async function loadAll() {
  loading.value = true
  try {
    const scheduleRes = await api.get(
      '/adventurer-s1/admin/expedition/config',
    )
    if (scheduleRes.data?.code === 200) Object.assign(form, scheduleRes.data.data)
  } catch (error: any) {
    message.error(error?.response?.data?.message || '运行设置加载失败')
  } finally {
    loading.value = false
  }
}

function addWindow() { form.ai_blackout_windows.push({ start: '09:00', end: '12:00' }) }
function removeWindow(index: number) { form.ai_blackout_windows.splice(index, 1) }

async function saveSchedule() {
  if (form.party_min_size > form.party_max_size) {
    message.error('队伍最小人数不能大于最大人数')
    return
  }
  savingSchedule.value = true
  try {
    const payload: Record<string, unknown> = { ...form }
    delete payload.ai_calls_enabled
    delete payload.daily_expedition_used
    const { data } = await api.post('/adventurer-s1/admin/expedition/config', payload)
    if (data?.code !== 200) throw new Error(data?.message || '保存失败')
    message.success('运行设置已保存，调度间隔已热更新')
    await loadAll()
  } catch (error: any) {
    message.error(error?.response?.data?.message || error?.message || '保存失败')
  } finally {
    savingSchedule.value = false
  }
}

async function loadDiagnosis() {
  diagnosing.value = true
  try {
    const { data } = await api.get('/adventurer-s1/admin/expedition/dispatch-diagnosis')
    if (data?.code !== 200) throw new Error(data?.message || '诊断失败')
    diagnosis.value = data.data
  } catch (error: any) {
    message.error(error?.response?.data?.message || error?.message || '诊断失败')
  } finally {
    diagnosing.value = false
  }
}

onMounted(loadAll)
</script>

<style scoped lang="scss">
.runtime-settings { display: flex; flex-direction: column; gap: 12px; min-width: 0; max-width: 100%; }
.settings-toolbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.settings-toolbar h6, .settings-card h6 { margin: 0 0 4px; color: var(--color-text); }
.settings-toolbar p, .settings-hint { margin: 0; color: var(--color-text-secondary, #718096); font-size: 12px; }
.settings-card { min-width: 0; max-width: 100%; padding: 14px; border: 1px solid var(--color-border); border-radius: 10px; background: var(--color-bg); overflow-wrap: anywhere; }
.field-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(190px, 100%), 1fr)); gap: 10px; margin-top: 10px; }
.field-grid.compact { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.field-grid label { display: flex; flex-direction: column; gap: 5px; color: var(--color-text-secondary, #718096); font-size: 12px; }
input, select { box-sizing: border-box; width: 100%; min-height: 36px; padding: 6px 9px; border: 1px solid var(--color-border); border-radius: 7px; background: var(--color-surface, var(--color-bg)); color: var(--color-text); }
.period-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.toggle-row { display: flex; align-items: center; gap: 8px; margin: 8px 0; color: var(--color-text); font-size: 13px; }
.toggle-row input { width: auto; min-height: 0; }
.muted { opacity: 0.55; }
.danger-zone { border-color: color-mix(in srgb, #dc3545 42%, var(--color-border)); }
.window-list { display: flex; flex-direction: column; gap: 7px; margin: 10px 0; }
.window-row { display: grid; grid-template-columns: minmax(110px, 1fr) auto minmax(110px, 1fr) auto; align-items: center; gap: 8px; }
.add-window { align-self: flex-start; }
.save-row { display: flex; justify-content: flex-end; }
.settings-state { padding: 28px; color: var(--color-text-secondary, #718096); text-align: center; }
.compact-toolbar { align-items: center; }
.diagnosis-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; margin-top: 10px; }
.diagnosis-grid span { padding: 8px 10px; border: 1px solid var(--color-border); border-radius: 7px; color: var(--color-text); font-size: 12px; }
@media (max-width: 720px) {
  .settings-toolbar { flex-wrap: wrap; }
  .settings-toolbar button, .save-row button { width: 100%; min-height: 40px; }
  .period-grid, .field-grid.compact { grid-template-columns: 1fr; }
  .window-row { grid-template-columns: 1fr auto 1fr; }
  .window-row button { grid-column: 1 / -1; }
}
</style>
