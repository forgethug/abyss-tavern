<template>
  <section class="fate-panel">
    <header class="fate-head">
      <div>
        <span class="fate-kicker">15级 · 命途突破</span>
        <h4>命途旅程</h4>
      </div>
      <div class="fate-head-actions">
        <span v-if="journey" class="fate-status">{{ statusLabel }}</span>
        <button
          v-if="canCollapseCompleted"
          type="button"
          class="fate-collapse"
          :aria-expanded="!completedCollapsed"
          :aria-controls="fateBodyId"
          @click="completedCollapsed = !completedCollapsed"
        >{{ completedCollapsed ? '展开回顾' : '收起命途' }}</button>
      </div>
    </header>

    <div v-if="canCollapseCompleted && completedCollapsed" class="fate-collapsed-summary">
      <strong>{{ journey?.route_title || '命途已经完成' }}</strong>
      <span>旅程已封卷 · {{ journey?.key_event_count ?? 0 }} 次关键经历</span>
    </div>

    <div :id="fateBodyId" v-show="!canCollapseCompleted || !completedCollapsed" class="fate-body">
      <div v-if="loading" class="fate-muted">正在倾听命途的回声…</div>
      <div v-else-if="errorText" class="fate-muted">{{ errorText }}</div>
      <div v-else-if="state && !state.eligible" class="fate-muted">
        {{ state.unavailable_reason === 'dead'
          ? '这名冒险者的生命已经终结，无法再开启或继续命途。'
          : `达到15级后，${state.npc_name}会带来一次命途抉择。` }}
      </div>

      <template v-else-if="journey">
        <div v-if="state?.reset_request?.status === 'pending'" class="reset-request" role="alert">
          <strong>管理员请求重置这段命途</strong>
          <p>{{ state.reset_request.reason || '该命途候选可能出现了显示与实际选择不一致的问题。' }}</p>
          <small>接受后会清空当前命途进度与命途事件，并自动重新生成三条候选道路；既有探险的普通奖励不会回滚。冒险者必须先退出队伍并回到休息状态。</small>
          <div class="reset-actions">
            <button class="fate-primary" :disabled="busy" @click="respondReset(true)">接受并重新生成</button>
            <button class="fate-secondary" :disabled="busy" @click="respondReset(false)">拒绝，保留当前命途</button>
          </div>
        </div>
        <template v-if="journey.status === 'awaiting_choice'">
          <p class="npc-line">{{ state?.npc_name }}：有些道路只能由他亲自走过，而你可以决定愿意陪他追寻哪一种愿望。</p>
          <template v-if="!journey.choice_options?.length">
            <div class="fate-notice" role="note">
              <strong>启程前请留意</strong>
              <span>命途会以冒险者此刻的人生经历为底稿，开始生成后便无法修改。若还有想留下的重要经历，不妨先让它们沉淀，再来叩问未来。</span>
            </div>
            <button class="fate-primary" :disabled="busy" @click="generateChoices">
              {{ busy ? '正在回顾经历…' : '与冒险者谈谈未来' }}
            </button>
          </template>
          <template v-else>
            <p class="reflection">{{ journey.reflection }}</p>
            <div class="choice-list">
              <button v-for="option in journey.choice_options" :key="option.id" class="choice-card" :disabled="busy" @click="choose(option.id)">
                <strong>{{ option.title }}</strong>
                <span>{{ option.summary }}</span>
                <em>我愿意陪你追寻这条路</em>
              </button>
            </div>
          </template>
        </template>

        <template v-else>
          <h5>{{ journey.route_title }}</h5>
          <p>{{ journey.route_summary }}</p>
          <div class="fate-progress">
            <span>关键经历 {{ journey.key_event_count }} / {{ state?.key_events_required ?? 8 }}</span>
            <span>相关探险 {{ journey.related_expedition_count }}</span>
            <span>踏足 {{ journey.visited_dungeons?.length ?? 0 }} 个地区</span>
          </div>
          <div v-if="journey.current_clue" class="clue-box">
            <b>追查目标</b>
            <span>{{ journey.current_clue }}</span>
          </div>
          <div v-if="journey.candidate_dungeons?.length && journey.status !== 'completed'" class="candidate-box">
            <b>可推进地点 · 任一均可</b>
            <span v-for="name in journey.candidate_dungeons" :key="name">{{ name }}</span>
          </div>
          <p v-if="journey.candidate_dungeons?.length && journey.status !== 'completed'" class="fate-location-note" role="note">
            下列任一地点都能推进当前命途，可按队伍与副本情况自由选择。
          </p>
          <p v-if="journey.candidate_dungeons?.length && journey.status !== 'completed'" class="fate-risk-note" role="note">
            地点保留原有生态难度；若触发命途关键事件或最终试炼，事件按 15 级命途强度展开，
            本场普通奖励与经验也按 15 级标准结算。普通追查和线索回响仍按原副本标准处理。
          </p>
          <div v-if="journey.breakthrough_completed" class="breakthrough">命途突破已经完成，未来等级上限开放后可继续成长。</div>
          <div v-if="journey.events?.length" class="fate-events">
            <article v-for="event in journey.events.slice().reverse().slice(0, 6)" :key="event.id">
              <small>{{ event.dungeon_name }}</small>
              <strong>{{ event.title }}</strong>
              <p>{{ event.summary }}</p>
            </article>
          </div>
        </template>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useApi } from '@/composables/useApi'
import { useGlobalMessage } from '@/composables/useGlobalMessage'

interface FateOption { id: string; title: string; summary: string }
interface FateEvent { id: number; dungeon_name: string; title: string; summary: string }
interface FateResetRequest { id: number; status: string; reason: string; requested_at?: string }
interface FateJourney {
  status: string
  route_title?: string
  route_summary?: string
  reflection?: string
  choice_options?: FateOption[]
  current_clue?: string
  candidate_dungeons?: string[]
  key_event_count: number
  related_expedition_count: number
  visited_dungeons?: string[]
  breakthrough_completed: boolean
  events?: FateEvent[]
}
interface FateState {
  eligible: boolean
  npc_name: string
  unavailable_reason?: 'dead'
  key_events_required?: number
  choice_generation_in_progress?: boolean
  journey?: FateJourney
  reset_request?: FateResetRequest | null
}

const props = defineProps<{ adventurerId: number }>()
const api = useApi()
const message = useGlobalMessage()
const state = ref<FateState | null>(null)
const loading = ref(true)
const busy = ref(false)
const errorText = ref('')
const journey = computed(() => state.value?.journey ?? null)
const completedCollapsed = ref(false)
const isCompleted = computed(() => journey.value?.status === 'completed')
const canCollapseCompleted = computed(() => (
  isCompleted.value && state.value?.reset_request?.status !== 'pending'
))
const fateBodyId = computed(() => `fate-journey-body-${props.adventurerId}`)
const statusLabel = computed(() => ({
  awaiting_choice: '待抉择', pursuing: '追寻中', final_ready: '最终试炼', completed: '已突破',
}[journey.value?.status ?? ''] ?? '命途中'))

watch(isCompleted, completed => {
  completedCollapsed.value = completed
}, { immediate: true })

watch(() => state.value?.reset_request?.status, status => {
  if (status === 'pending') completedCollapsed.value = false
})

async function load() {
  loading.value = true
  errorText.value = ''
  try {
    const res = await api.get(`/adventurer-s1/fate/${props.adventurerId}`)
    state.value = res.data?.data ?? null
  } catch (error: any) {
    errorText.value = error?.response?.data?.message || '暂时无法读取命途旅程'
  } finally {
    loading.value = false
  }
}

async function generateChoices() {
  busy.value = true
  try {
    await api.post(`/adventurer-s1/fate/${props.adventurerId}/choices`, {})
    message.success('命途的回响正在浮现，你可以先去处理其他事情')
    // 后端立即返回并由 worker 调用 AI；这里短轮询状态，不占用服务端请求线程。
    for (let attempt = 0; attempt < 80; attempt += 1) {
      await new Promise(resolve => window.setTimeout(resolve, 1500))
      const res = await api.get(`/adventurer-s1/fate/${props.adventurerId}`)
      state.value = res.data?.data ?? state.value
      if (state.value?.unavailable_reason === 'dead') {
        message.error('冒险者已经死亡，无法继续命途')
        return
      }
      if ((state.value?.journey?.choice_options?.length ?? 0) > 0) return
      if (state.value?.choice_generation_in_progress === false) {
        message.error('命途暂时没有回应，请稍后再试')
        return
      }
    }
    message.info('命途的回响尚未完全浮现，稍后重新打开即可查看')
  } catch (error: any) {
    message.error(error?.response?.data?.message || '命途的回响暂时没有回应，请稍后重试')
  } finally {
    busy.value = false
  }
}

async function choose(optionId: string) {
  if (!window.confirm('确认陪伴冒险者追寻这条道路？正式开始后不能直接重选。')) return
  busy.value = true
  try {
    const res = await api.post(`/adventurer-s1/fate/${props.adventurerId}/choose`, { option_id: optionId })
    if (state.value) state.value.journey = res.data?.data
    message.success('命途旅程已经开始')
  } catch (error: any) {
    message.error(error?.response?.data?.message || '暂时无法踏上这条命途，请稍后重试')
  } finally {
    busy.value = false
  }
}

async function respondReset(accept: boolean) {
  const requestId = state.value?.reset_request?.id
  if (!requestId || busy.value) return
  const prompt = accept
    ? '确认重置当前命途？命途进度与命途事件会被清空，随后重新生成三条候选道路。'
    : '确认拒绝本次重置并保留当前命途？'
  if (!window.confirm(prompt)) return
  busy.value = true
  let regenerate = false
  try {
    const res = await api.post(`/adventurer-s1/fate/${props.adventurerId}/reset-request/respond`, {
      request_id: requestId,
      accept,
    })
    if (state.value) {
      state.value.journey = res.data?.data?.journey ?? state.value.journey
      state.value.reset_request = null
    }
    regenerate = accept && !!res.data?.data?.accepted
    message.success(accept ? '命途已经重置，正在重新生成候选' : '已保留当前命途旅程')
  } catch (error: any) {
    message.error(error?.response?.data?.message || '暂时无法处理命途重置确认')
  } finally {
    busy.value = false
  }
  if (regenerate) await generateChoices()
}

onMounted(load)
</script>

<style scoped>
.fate-panel {
  position: relative;
  overflow: hidden;
  --fate-accent: #b77be8;
  --fate-accent-strong: #d7b5f3;
  padding: 16px;
  border: 1px solid #765394;
  border: 1px solid color-mix(in srgb, var(--fate-accent) 46%, var(--color-border, #30363d));
  border-radius: 14px;
  background-color: var(--color-surface, #161b22);
  background:
    radial-gradient(circle at 92% -10%, color-mix(in srgb, var(--fate-accent) 22%, transparent), transparent 38%),
    linear-gradient(145deg, color-mix(in srgb, var(--fate-accent) 7%, var(--color-surface, #161b22)), var(--color-surface, #161b22) 58%);
  color: var(--color-text, #e6edf3);
  box-shadow: inset 0 1px color-mix(in srgb, var(--color-text, #e6edf3) 7%, transparent), 0 8px 24px rgba(38, 20, 55, .12);
}
.fate-panel::before {
  content: '';
  position: absolute;
  top: 0;
  right: 0;
  width: 96px;
  height: 72px;
  pointer-events: none;
  opacity: .55;
  background-image: radial-gradient(circle, var(--fate-accent) 1.2px, transparent 1.4px);
  background-size: 14px 14px;
  mask-image: linear-gradient(135deg, #000, transparent 75%);
}
.fate-panel h4,
.fate-panel h5,
.fate-panel p,
.fate-panel strong,
.fate-panel b { color: var(--color-text, #e6edf3); }
.fate-head { position: relative; display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; border-bottom: 1px solid color-mix(in srgb, var(--fate-accent) 20%, transparent); margin-bottom: 12px; }
.fate-head h4 { margin: 2px 0 12px; }
.fate-kicker { font-size: 11px; color: var(--fate-accent-strong); font-weight: 800; letter-spacing: .1em; }
.fate-head-actions { position: relative; display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 7px; }
.fate-status { position: relative; padding: 4px 10px; border: 1px solid color-mix(in srgb, var(--fate-accent) 50%, var(--color-border, #30363d)); border-radius: 999px; background: color-mix(in srgb, var(--fate-accent) 13%, var(--color-surface, #161b22)); color: var(--fate-accent-strong); font-size: 12px; font-weight: 800; box-shadow: 0 2px 8px color-mix(in srgb, var(--fate-accent) 12%, transparent); }
.fate-collapse { padding: 4px 9px; border: 1px solid color-mix(in srgb, var(--fate-accent) 38%, var(--color-border, #30363d)); border-radius: 999px; background: var(--color-bg, #0d1117); color: var(--fate-accent-strong); font-size: 11px; font-weight: 700; cursor: pointer; }
.fate-collapse:hover { border-color: var(--fate-accent); }
.fate-collapse:focus-visible { outline: 2px solid var(--fate-accent); outline-offset: 2px; }
.fate-collapsed-summary { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 6px 12px; padding: 4px 1px 2px; }
.fate-collapsed-summary strong { color: var(--fate-accent-strong); font-size: 13px; }
.fate-collapsed-summary span { color: var(--color-text-secondary, #8b949e); font-size: 12px; }
.npc-line,
.reflection,
.fate-muted { color: var(--color-text-secondary, #8b949e); line-height: 1.7; }
.fate-notice { display: grid; gap: 4px; margin: 10px 0 12px; padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--fate-accent) 35%, var(--color-border, #30363d)); border-radius: 10px; background: color-mix(in srgb, var(--fate-accent) 9%, var(--color-bg, #0d1117)); box-shadow: inset 3px 0 var(--fate-accent); }
.fate-notice strong { font-size: 13px; color: var(--fate-accent-strong); }
.fate-notice span { color: var(--color-text-secondary, #8b949e); font-size: 12px; line-height: 1.65; }
.reset-request { display: grid; gap: 7px; margin-bottom: 14px; padding: 12px; border: 1px solid color-mix(in srgb, var(--color-warning, #d29922) 62%, var(--color-border, #30363d)); border-radius: 10px; background: color-mix(in srgb, var(--color-warning, #d29922) 10%, var(--color-bg, #0d1117)); }
.reset-request p { margin: 0; line-height: 1.55; }
.reset-request small { color: var(--color-text-secondary, #8b949e); line-height: 1.55; }
.reset-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 3px; }
.fate-secondary { padding: 9px 14px; border: 1px solid var(--color-border, #30363d); border-radius: 10px; background: transparent; color: var(--color-text, #e6edf3); cursor: pointer; }
.fate-primary,
.choice-card { border: 1px solid var(--color-border, #30363d); background: var(--color-bg, #0d1117); color: var(--color-text, #e6edf3); border-radius: 10px; cursor: pointer; }
.fate-primary { padding: 9px 14px; }
.choice-list { display: grid; gap: 10px; }
.choice-card { padding: 12px; text-align: left; display: grid; gap: 6px; }
.choice-card:hover { border-color: var(--color-primary, #58a6ff); background: var(--color-hover, rgba(255,255,255,.06)); }
.choice-card span { font-size: 13px; color: var(--color-text-secondary, #8b949e); line-height: 1.55; }
.choice-card em { font-size: 12px; color: var(--color-primary, #58a6ff); font-style: normal; }
.fate-progress { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; }
.fate-progress span,
.candidate-box span { font-size: 12px; padding: 5px 9px; border: 1px solid color-mix(in srgb, var(--fate-accent) 28%, var(--color-border, #30363d)); border-radius: 8px; background: color-mix(in srgb, var(--fate-accent) 8%, var(--color-bg, #0d1117)); color: var(--color-text, #e6edf3); }
.clue-box,
.candidate-box { display: flex; gap: 8px; align-items: flex-start; margin-top: 10px; padding: 11px; border: 1px solid color-mix(in srgb, var(--fate-accent) 26%, var(--color-border, #30363d)); border-radius: 10px; background: color-mix(in srgb, var(--fate-accent) 5%, var(--color-bg, #0d1117)); color: var(--color-text, #e6edf3); box-shadow: inset 3px 0 var(--fate-accent); }
.clue-box span { color: var(--color-text, #e6edf3); line-height: 1.55; }
.candidate-box { flex-wrap: wrap; }
.fate-location-note,
.fate-risk-note { margin: 8px 2px 0; color: var(--color-text-secondary, #8b949e); font-size: 12px; line-height: 1.55; }
.fate-location-note { color: var(--fate-accent-strong); font-weight: 650; }
.breakthrough { margin-top: 12px; padding: 10px; border-radius: 9px; background: rgba(83,185,120,.16); color: var(--color-success, #3fb950); }
.fate-events { margin-top: 14px; display: grid; gap: 8px; }
.fate-events article { padding-left: 10px; border-left: 2px solid var(--color-primary, #58a6ff); }
.fate-events small { display: block; color: var(--color-text-secondary, #8b949e); }
.fate-events strong { font-size: 13px; }
.fate-events p { margin: 4px 0; font-size: 12px; color: var(--color-text-secondary, #8b949e); line-height: 1.5; }
.choice-card:disabled,
.fate-primary:disabled,
.fate-secondary:disabled { opacity: .55; cursor: not-allowed; }
:global([data-theme="light"] .fate-panel) {
  --fate-accent: #8350b8;
  --fate-accent-strong: #663092;
  border-color: #a98ac8;
  background:
    radial-gradient(circle at 100% 0, rgba(132, 79, 191, .16), transparent 42%),
    linear-gradient(145deg, #fcfaff, #f3edf9);
  box-shadow: 0 10px 28px rgba(78, 45, 112, .11);
  color: #21172a;
}

:global([data-theme="light"] .fate-head) {
  border-bottom: 1px solid rgba(111, 68, 153, .14);
  margin-bottom: 12px;
}

:global([data-theme="light"] .fate-head h4),
:global([data-theme="light"] .fate-panel h5),
:global([data-theme="light"] .fate-panel strong),
:global([data-theme="light"] .fate-panel b) { color: #281635; }

:global([data-theme="light"] .fate-kicker),
:global([data-theme="light"] .choice-card em) {
  color: #6f369c;
  font-weight: 700;
}

:global([data-theme="light"] .fate-status) {
  border: 1px solid #cbb3e3;
  background: #eadff5;
  color: #57277b;
  font-weight: 700;
}

:global([data-theme="light"] .fate-notice) {
  border-color: #d4c0e6;
  background: #f6effb;
  box-shadow: inset 3px 0 #8350b8;
}

:global([data-theme="light"] .fate-notice strong) { color: #663092; }
:global([data-theme="light"] .fate-notice span) { color: #45384f; }

:global([data-theme="light"] .npc-line),
:global([data-theme="light"] .reflection),
:global([data-theme="light"] .fate-muted),
:global([data-theme="light"] .choice-card span),
:global([data-theme="light"] .fate-events p) { color: #45384f; }

:global([data-theme="light"] .fate-primary),
:global([data-theme="light"] .choice-card) {
  border-color: #c6addd;
  background: rgba(255, 255, 255, .82);
  color: #302039;
  box-shadow: 0 3px 10px rgba(78, 45, 112, .07);
}

:global([data-theme="light"] .fate-primary:hover),
:global([data-theme="light"] .choice-card:hover) {
  border-color: #7d45a8;
  background: #f2e7fb;
  box-shadow: 0 6px 16px rgba(78, 45, 112, .13);
}

:global([data-theme="light"] .fate-progress span),
:global([data-theme="light"] .candidate-box span) {
  border: 1px solid #dacbe8;
  background: #eee5f6;
  color: #4d315e;
}

:global([data-theme="light"] .clue-box),
:global([data-theme="light"] .candidate-box) {
  border: 1px solid #ded1ea;
  background: rgba(255, 255, 255, .68);
}

:global([data-theme="light"] .breakthrough) {
  border: 1px solid #a9d8b7;
  background: #e4f5e9;
  color: #176b35;
  font-weight: 600;
}

:global([data-theme="light"] .fate-events article) {
  border-left-color: #8952b2;
  background: rgba(255, 255, 255, .46);
  border-radius: 0 8px 8px 0;
  padding: 8px 10px;
}

:global([data-theme="light"] .fate-events small) { color: #735c7f; }
@media (max-width: 640px) {
  .fate-panel { padding: 12px; }
  .fate-head { flex-wrap: wrap; }
  .fate-head-actions { margin-left: auto; }
  .npc-line, .reflection, .fate-muted, .choice-card span, .fate-events p, .fate-notice span { font-size: 14px; line-height: 1.7; }
  .reset-actions > button { flex: 1; white-space: normal; }
}
</style>
