<template>
  <Teleport to="body">
    <div class="squad-modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="closeModal">
      <section
        ref="modalRef"
        class="squad-modal s1-mobile-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="squad-modal-title"
        tabindex="-1"
        @keydown="onDialogKeydown"
      >
        <header class="squad-modal-header s1-mobile-dialog-header">
          <div>
            <p class="squad-modal-kicker">深渊酒馆 · 同袍印记</p>
            <h2 id="squad-modal-title">纹章小队</h2>
            <p>1名队长 + 最多{{ memberSlots }}名成员（总{{ totalCapacity }}人）</p>
          </div>
          <button
            ref="closeButtonRef"
            type="button"
            class="squad-close"
            aria-label="关闭纹章小队"
            :disabled="busy"
            @click="closeModal"
          >×</button>
        </header>

        <div class="squad-modal-body s1-mobile-dialog-body">
          <div v-if="store.squadsLoading && !feature" class="squad-loading">正在翻阅纹章名册…</div>
          <div v-else-if="!feature" class="squad-empty">
            <strong>纹章名册暂时无法展开</strong>
            <span>稍后再来酒馆柜台看看。</span>
            <button type="button" class="squad-btn secondary" @click="loadOverview(1)">重新翻阅</button>
          </div>

          <template v-else-if="store.mySquad">
            <div class="squad-current-hero">
              <SquadEmblem
                :emblem="store.mySquad.emblem"
                :size="76"
                :title="`${store.mySquad.name}的小队徽记`"
              />
              <div class="squad-current-copy">
                <span class="squad-seal-caption">{{ isLeader ? '你是执印队长' : '你已佩上此印' }}</span>
                <h3>{{ store.mySquad.name }}</h3>
                <p>
                  {{ currentMemberCount }} / {{ feature.total_capacity }} 人 ·
                  1名队长 + 最多{{ memberSlots }}名成员
                </p>
              </div>
            </div>

            <p v-if="!currentAdventurer" class="squad-identity-warning">
              当前名册与本人的冒险者档案尚未对上，请刷新后再进行队长或退队操作。
            </p>

            <section class="squad-panel">
              <div class="squad-section-head">
                <div>
                  <span class="squad-section-eyebrow">同袍名册</span>
                  <h3>佩印成员</h3>
                </div>
                <span class="squad-count">{{ currentMemberCount }}/{{ feature.total_capacity }}</span>
              </div>
              <ul v-if="store.mySquad.members?.length" class="squad-member-list">
                <li v-for="member in store.mySquad.members" :key="member.adventurer_id ?? member.id ?? member.name">
                  <span class="member-mark" aria-hidden="true">{{ memberIsLeader(member) ? '♛' : '✦' }}</span>
                  <span>{{ member.name }}</span>
                  <span v-if="memberIsLeader(member)" class="leader-badge">队长</span>
                </li>
              </ul>
              <p v-else class="squad-muted">名册尚在誊写，刷新后即可查看成员。</p>
            </section>

            <section v-if="isLeader" class="squad-panel">
              <div class="squad-section-head">
                <div>
                  <span class="squad-section-eyebrow">队长权限</span>
                  <h3>重绘小队徽记</h3>
                </div>
                <SquadEmblem :emblem="editEmblem" :size="48" title="新徽记预览" />
              </div>
              <SquadEmblemEditor v-model="editEmblem" :feature="feature" />
              <div class="squad-actions">
                <button type="button" class="squad-btn primary" :disabled="busy || !currentAdventurer" @click="saveEmblem">
                  {{ actionName === 'emblem' ? '正在封印…' : '保存新徽记' }}
                </button>
                <button type="button" class="squad-btn danger-ghost" :disabled="busy || !currentAdventurer" @click="disband">
                  解散纹章小队
                </button>
              </div>
            </section>

            <section v-else class="squad-panel squad-leave-panel">
              <div>
                <span class="squad-section-eyebrow">离开名册</span>
                <h3>卸下纹章</h3>
                <p>退出后，冒险者名片将不再佩戴这枚徽记；除此之外不会损失任何玩法权益。</p>
              </div>
              <button type="button" class="squad-btn danger-ghost" :disabled="busy || !currentAdventurer" @click="leave">
                {{ actionName === 'leave' ? '正在交还徽记…' : '退出小队' }}
              </button>
            </section>
          </template>

          <template v-else>
            <div class="squad-mode-tabs" role="group" aria-label="纹章小队操作">
              <button
                type="button"
                :class="{ active: mode === 'create' }"
                :aria-pressed="mode === 'create'"
                @click="mode = 'create'"
              >创立小队</button>
              <button
                type="button"
                :class="{ active: mode === 'browse' }"
                :aria-pressed="mode === 'browse'"
                @click="mode = 'browse'"
              >寻找同袍</button>
            </div>

            <section v-if="mode === 'create'" class="squad-create-layout">
              <div class="squad-preview-stage">
                <span class="preview-caption">柜台实时样稿</span>
                <SquadEmblem :emblem="createEmblem" :size="112" :title="createName || '新小队徽记预览'" />
                <strong>{{ createName.trim() || '尚未命名的小队' }}</strong>
                <p>这枚静态徽记只用于名片展示，不提供属性、战斗或探险加成。</p>
              </div>

              <div class="squad-create-form">
                <label class="squad-field">
                  <span>小队名称</span>
                  <input
                    v-model="createName"
                    type="text"
                    :maxlength="feature.name_max_length"
                    :placeholder="`最多 ${feature.name_max_length} 个字`"
                    autocomplete="off"
                  />
                  <small>{{ createName.trim().length }}/{{ feature.name_max_length }}</small>
                </label>

                <SquadEmblemEditor v-model="createEmblem" :feature="feature" />

                <div class="squad-cost-note">
                  <strong>创立费用：{{ formatMoney(feature.create_gold_cost) }} 金币</strong>
                  <span>确认创立后立即支付；小队仅提供共同徽记展示。</span>
                  <span>席位为1名队长 + 最多{{ memberSlots }}名成员（总{{ totalCapacity }}人）。</span>
                </div>

                <button
                  type="button"
                  class="squad-btn primary wide"
                  :disabled="busy || !canCreate"
                  @click="create"
                >
                  {{ actionName === 'create' ? '正在落印立册…' : `支付 ${formatMoney(feature.create_gold_cost)} 金币并创立` }}
                </button>
              </div>
            </section>

            <section v-else class="squad-browse">
              <div class="squad-browse-intro">
                <div>
                  <span class="squad-section-eyebrow">酒馆公开名册</span>
                  <h3>选择愿意共同佩印的小队</h3>
                </div>
                <button type="button" class="squad-btn secondary" :disabled="store.squadsLoading" @click="loadOverview(currentPage, true)">
                  {{ store.squadsLoading ? '翻阅中…' : '刷新名册' }}
                </button>
              </div>

              <div v-if="store.squadsLoading && !store.squadItems.length" class="squad-loading">正在翻阅纹章名册…</div>
              <div v-else-if="!store.squadItems.length" class="squad-empty compact">
                <strong>名册上还没有公开的小队</strong>
                <span>你可以成为第一位在柜台落印的队长。</span>
              </div>
              <div v-else class="squad-browse-grid">
                <article v-for="squad in store.squadItems" :key="squad.id" class="squad-browse-card">
                  <SquadEmblem :emblem="squad.emblem" :size="52" :title="`${squad.name}的小队徽记`" />
                  <div class="squad-browse-copy">
                    <h4>{{ squad.name }}</h4>
                    <p>队长：{{ squad.leader_name || '名册记录中' }}</p>
                    <span>{{ squadCount(squad) }}/{{ squadCapacity(squad) }} 人</span>
                  </div>
                  <button
                    type="button"
                    class="squad-btn secondary"
                    :disabled="busy || squad.is_full === true || squadCount(squad) >= squadCapacity(squad)"
                    @click="join(squad)"
                  >{{ squad.is_full === true || squadCount(squad) >= squadCapacity(squad) ? '名额已满' : '加入并佩印' }}</button>
                </article>
              </div>

              <nav v-if="pagination && pagination.pages > 1" class="squad-pagination" aria-label="纹章小队分页">
                <button type="button" :disabled="store.squadsLoading || !pagination.has_prev" @click="loadOverview(currentPage - 1)">上一页</button>
                <span>第 {{ currentPage }} / {{ pagination.pages }} 页</span>
                <button type="button" :disabled="store.squadsLoading || !pagination.has_next" @click="loadOverview(currentPage + 1)">下一页</button>
              </nav>
            </section>
          </template>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useAdventurerS1Store, type AdventurerData, type SquadData, type SquadEmblemData, type SquadMember } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { formatMoney } from '@/utils/format'
import SquadEmblem from './SquadEmblem.vue'
import SquadEmblemEditor from './SquadEmblemEditor.vue'
import { DEFAULT_SQUAD_EMBLEM, normalizeSquadEmblem } from '../squadEmblem'

const props = defineProps<{ adventurers: AdventurerData[] }>()
const emit = defineEmits<{ close: [] }>()
const store = useAdventurerS1Store()
const message = useGlobalMessage()
const mode = ref<'create' | 'browse'>('create')
const createName = ref('')
const createEmblem = ref<SquadEmblemData>({ ...DEFAULT_SQUAD_EMBLEM })
const editEmblem = ref<SquadEmblemData>({ ...DEFAULT_SQUAD_EMBLEM })
const actionName = ref<'create' | 'join' | 'leave' | 'emblem' | 'disband' | null>(null)
const busy = computed(() => actionName.value !== null)
const feature = computed(() => store.squadFeature)
const pagination = computed(() => store.squadPagination)
const currentPage = computed(() => pagination.value?.page ?? 1)
const memberSlots = computed(() => feature.value?.member_slots ?? 9)
const totalCapacity = computed(() => feature.value?.total_capacity ?? (memberSlots.value + 1))
const playableAdventurer = computed(
  () => props.adventurers.find(adv => adv.status !== 'dead' && adv.status !== 'archived') ?? null,
)
const squadAdventurer = computed(() => {
  const squad = store.mySquad
  if (!squad) return null
  const memberIds = new Set(
    (squad.members ?? [])
      .map(member => Number(member.adventurer_id ?? member.id))
      .filter(Number.isFinite),
  )
  return props.adventurers.find(adv => memberIds.has(adv.id))
    ?? props.adventurers.find(adv => adv.squad_badge?.squad_id === squad.id)
    ?? props.adventurers.find(
      adv => adv.id === squad.leader_adventurer_id || adv.id === squad.leader_id,
    )
    ?? null
})
const currentAdventurer = computed(() => (
  store.mySquad ? squadAdventurer.value : playableAdventurer.value
))
const currentMemberCount = computed(() => squadCount(store.mySquad))
const isLeader = computed(() => {
  const squad = store.mySquad
  const adventurer = currentAdventurer.value
  if (!squad || !adventurer) return false
  return squad.is_leader === true
    || squad.role === 'leader'
    || squad.leader_adventurer_id === adventurer.id
    || squad.leader_id === adventurer.id
})
const canCreate = computed(() => {
  const name = createName.value.trim()
  return !!currentAdventurer.value
    && name.length > 0
    && name.length <= (feature.value?.name_max_length ?? 0)
})

function squadCount(squad: SquadData | null): number {
  if (!squad) return 0
  return squad.member_count ?? squad.members?.length ?? 0
}

function squadCapacity(squad: SquadData): number {
  return squad.total_capacity ?? feature.value?.total_capacity ?? 10
}

function memberIsLeader(member: SquadMember): boolean {
  const squad = store.mySquad
  const memberId = member.adventurer_id ?? member.id
  return member.is_leader === true
    || member.role === 'leader'
    || memberId === squad?.leader_adventurer_id
    || memberId === squad?.leader_id
}

function actionError(error: any, fallback: string) {
  message.error(error?.response?.data?.message || error?.message || fallback)
}

function announceMutationSuccess(result: any, successText: string) {
  message.success(successText)
  if (result?.squad_refresh?.ok === false) {
    message.warning(result.squad_refresh.message || '操作已完成，但名册刷新失败，请稍后重新打开查看')
  }
}

async function loadOverview(page: number, force = false) {
  try {
    const result = await store.fetchSquads(Math.max(1, page), pagination.value?.per_page ?? 8, force)
    if (result.code !== 200) message.error(result.message || '纹章名册暂时无法展开')
  } catch (error: any) {
    actionError(error, '纹章名册暂时无法展开')
  }
}

async function create() {
  if (!canCreate.value || !currentAdventurer.value || busy.value) return
  actionName.value = 'create'
  try {
    const result = await store.createSquad(
      currentAdventurer.value.id,
      createName.value.trim(),
      normalizeSquadEmblem(createEmblem.value),
    )
    if (result.code === 200 || result.code === 201) announceMutationSuccess(result, '纹章已落印，小队名册正式建立')
    else message.error(result.message || '创立纹章小队失败')
  } catch (error: any) {
    actionError(error, '创立纹章小队失败')
  } finally {
    actionName.value = null
  }
}

async function join(squad: SquadData) {
  if (!currentAdventurer.value || busy.value) return
  actionName.value = 'join'
  try {
    const result = await store.joinSquad(squad.id, currentAdventurer.value.id)
    if (result.code === 200) announceMutationSuccess(result, `已加入「${squad.name}」，名片将佩戴共同徽记`)
    else message.error(result.message || '加入纹章小队失败')
  } catch (error: any) {
    actionError(error, '加入纹章小队失败')
  } finally {
    actionName.value = null
  }
}

async function leave() {
  if (!store.mySquad || !currentAdventurer.value || busy.value) return
  if (!window.confirm(`确定退出「${store.mySquad.name}」并卸下纹章吗？`)) return
  actionName.value = 'leave'
  try {
    const result = await store.leaveSquad(store.mySquad.id, currentAdventurer.value.id)
    if (result.code === 200) announceMutationSuccess(result, '已退出小队，纹章已从名片卸下')
    else message.error(result.message || '退出纹章小队失败')
  } catch (error: any) {
    actionError(error, '退出纹章小队失败')
  } finally {
    actionName.value = null
  }
}

async function saveEmblem() {
  if (!store.mySquad || !currentAdventurer.value || busy.value) return
  actionName.value = 'emblem'
  try {
    const result = await store.updateSquadEmblem(
      store.mySquad.id,
      currentAdventurer.value.id,
      normalizeSquadEmblem(editEmblem.value),
    )
    if (result.code === 200) announceMutationSuccess(result, '新徽记已传给所有佩印成员')
    else message.error(result.message || '保存徽记失败')
  } catch (error: any) {
    actionError(error, '保存徽记失败')
  } finally {
    actionName.value = null
  }
}

async function disband() {
  if (!store.mySquad || !currentAdventurer.value || busy.value) return
  if (!window.confirm(`确定解散「${store.mySquad.name}」吗？所有成员都会卸下纹章，此操作不可撤销。`)) return
  actionName.value = 'disband'
  try {
    const result = await store.disbandSquad(store.mySquad.id, currentAdventurer.value.id)
    if (result.code === 200) announceMutationSuccess(result, '纹章小队已解散，名册已经封存')
    else message.error(result.message || '解散纹章小队失败')
  } catch (error: any) {
    actionError(error, '解散纹章小队失败')
  } finally {
    actionName.value = null
  }
}

function closeModal() {
  if (!busy.value) emit('close')
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') closeModal()
}

const modalRef = ref<HTMLElement | null>(null)
const closeButtonRef = ref<HTMLButtonElement | null>(null)
let previouslyFocusedElement: HTMLElement | null = null
let previousBodyOverflow = ''

function focusableElements(): HTMLElement[] {
  if (!modalRef.value) return []
  return Array.from(modalRef.value.querySelectorAll<HTMLElement>(
    'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
  )).filter(element => element.tabIndex >= 0 && element.getAttribute('aria-hidden') !== 'true')
}

function onDialogKeydown(event: KeyboardEvent) {
  if (event.key !== 'Tab') return
  const elements = focusableElements()
  if (elements.length === 0) {
    event.preventDefault()
    modalRef.value?.focus()
    return
  }
  const first = elements[0]
  const last = elements[elements.length - 1]
  if (
    event.shiftKey
    && (document.activeElement === first || document.activeElement === modalRef.value)
  ) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

onMounted(async () => {
  previouslyFocusedElement = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null
  previousBodyOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'
  window.addEventListener('keydown', onKeydown)
  await nextTick()
  closeButtonRef.value?.focus()
  try {
    const refresh = await store.refreshSquadsAndAdventurers(1, 8)
    if (!refresh.ok) {
      message.warning(refresh.message || '纹章名册刷新失败，请稍后重试')
    }
  } catch (error: any) {
    actionError(error, '纹章名册暂时无法展开')
  }
})

onBeforeUnmount(() => {
  document.body.style.overflow = previousBodyOverflow
  window.removeEventListener('keydown', onKeydown)
  const returnTarget = previouslyFocusedElement
  if (returnTarget && document.contains(returnTarget)) {
    requestAnimationFrame(() => returnTarget.focus())
  }
})

watch(feature, nextFeature => {
  if (!nextFeature) return
  createEmblem.value = normalizeSquadEmblem(nextFeature.catalog?.default_emblem)
}, { immediate: true })

watch(() => store.mySquad?.emblem, emblem => {
  if (emblem) editEmblem.value = normalizeSquadEmblem(emblem)
}, { immediate: true })
</script>

<style scoped lang="scss">
.squad-modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1300;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(8, 7, 12, 0.78);
  backdrop-filter: blur(5px);
}

.squad-modal {
  width: min(920px, 100%);
  max-height: min(880px, calc(100vh - 40px));
  overflow: hidden;
  color: var(--color-text);
  background:
    radial-gradient(circle at 12% 0, rgba(190, 136, 65, 0.13), transparent 34%),
    radial-gradient(circle at 95% 100%, rgba(91, 67, 131, 0.13), transparent 36%),
    var(--color-surface);
  border: 1px solid color-mix(in srgb, #c89b58 48%, var(--color-border));
  border-radius: 18px;
  box-shadow: 0 24px 70px rgba(0, 0, 0, 0.48);
}

.squad-modal-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  padding: 22px 24px 18px;
  border-bottom: 1px solid color-mix(in srgb, #c89b58 28%, var(--color-border));
  background: linear-gradient(100deg, rgba(92, 57, 35, 0.24), transparent);

  h2 {
    margin: 2px 0 4px;
    color: var(--color-text);
    font-family: Georgia, 'Noto Serif SC', serif;
    font-size: 25px;
    letter-spacing: 0.08em;
  }

  p {
    margin: 0;
    color: var(--color-text-secondary);
    font-size: 13px;
  }

  .squad-modal-kicker {
    color: #c89b58;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.16em;
  }
}

.squad-close {
  width: 38px;
  height: 38px;
  flex: 0 0 auto;
  color: var(--color-text-secondary);
  background: color-mix(in srgb, var(--color-bg) 84%, transparent);
  border: 1px solid var(--color-border);
  border-radius: 50%;
  font-size: 25px;
  line-height: 1;
  cursor: pointer;

  &:hover:not(:disabled) {
    color: #e3bd67;
    border-color: #c89b58;
  }
}

.squad-modal-body {
  max-height: calc(100vh - 160px);
  overflow-y: auto;
  padding: 22px 24px 26px;
}

.squad-loading,
.squad-empty {
  display: flex;
  min-height: 240px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--color-text-secondary);
  text-align: center;

  strong { color: var(--color-text); }
  &.compact { min-height: 180px; }
}

.squad-current-hero {
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 18px;
  margin-bottom: 16px;
  background: linear-gradient(120deg, rgba(200, 155, 88, 0.13), rgba(83, 56, 115, 0.08));
  border: 1px solid color-mix(in srgb, #c89b58 32%, var(--color-border));
  border-radius: 14px;
}

.squad-current-copy {
  min-width: 0;

  h3 {
    margin: 3px 0 5px;
    color: var(--color-text);
    font-family: Georgia, 'Noto Serif SC', serif;
    font-size: 24px;
  }

  p {
    margin: 0;
    color: var(--color-text-secondary);
    font-size: 13px;
  }
}

.squad-seal-caption,
.squad-section-eyebrow,
.preview-caption {
  color: #b98b4d;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.1em;
}

.squad-panel {
  padding: 18px;
  margin-top: 14px;
  background: color-mix(in srgb, var(--color-bg) 58%, transparent);
  border: 1px solid var(--color-border);
  border-radius: 14px;
}

.squad-section-head,
.squad-browse-intro {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  margin-bottom: 14px;

  h3 {
    margin: 2px 0 0;
    color: var(--color-text);
    font-size: 16px;
  }
}

.squad-count,
.leader-badge {
  padding: 3px 8px;
  color: #d6aa66;
  background: rgba(200, 155, 88, 0.1);
  border: 1px solid rgba(200, 155, 88, 0.32);
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
}

.squad-member-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  padding: 0;
  margin: 0;
  list-style: none;

  li {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 8px;
    padding: 9px 11px;
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: 9px;
    font-size: 13px;
  }
}

.member-mark { color: #c89b58; }
.leader-badge { margin-left: auto; }
.squad-muted { margin: 0; color: var(--color-text-secondary); font-size: 13px; }
.squad-identity-warning {
  padding: 10px 12px;
  margin: 0 0 14px;
  color: #d6aa66;
  background: rgba(183, 126, 59, 0.09);
  border: 1px solid rgba(200, 155, 88, 0.3);
  border-radius: 9px;
  font-size: 12px;
}

.squad-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 16px;
}

.squad-leave-panel {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;

  h3 { margin: 2px 0 5px; color: var(--color-text); font-size: 16px; }
  p { margin: 0; color: var(--color-text-secondary); font-size: 13px; }
}

.squad-mode-tabs {
  display: flex;
  gap: 6px;
  padding: 4px;
  margin-bottom: 18px;
  background: color-mix(in srgb, var(--color-bg) 72%, transparent);
  border: 1px solid var(--color-border);
  border-radius: 11px;

  button {
    flex: 1;
    min-height: 42px;
    color: var(--color-text-secondary);
    background: transparent;
    border: 0;
    border-radius: 8px;
    font-weight: 700;
    cursor: pointer;
  }

  button.active {
    color: #e2b96d;
    background: color-mix(in srgb, #b98145 16%, var(--color-surface));
    box-shadow: inset 0 0 0 1px rgba(200, 155, 88, 0.3);
  }
}

.squad-create-layout {
  display: grid;
  grid-template-columns: minmax(240px, 0.78fr) minmax(380px, 1.35fr);
  gap: 20px;
}

.squad-preview-stage {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  min-height: 310px;
  align-self: start;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  gap: 14px;
  padding: 22px;
  text-align: center;
  background:
    radial-gradient(circle at 50% 42%, rgba(200, 155, 88, 0.16), transparent 31%),
    linear-gradient(145deg, rgba(45, 31, 43, 0.17), rgba(84, 54, 39, 0.08)),
    var(--color-surface);
  border: 1px solid color-mix(in srgb, #c89b58 28%, var(--color-border));
  border-radius: 14px;

  strong {
    color: var(--color-text);
    font-family: Georgia, 'Noto Serif SC', serif;
    font-size: 19px;
  }

  p {
    max-width: 260px;
    margin: 0;
    color: var(--color-text-secondary);
    font-size: 12px;
    line-height: 1.65;
  }
}

.squad-create-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.squad-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  color: var(--color-text-secondary);
  font-size: 12px;

  input {
    width: 100%;
    min-height: 42px;
    box-sizing: border-box;
    padding: 8px 11px;
    color: var(--color-text);
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: 8px;
    outline: none;
  }

  input:focus {
    border-color: #c89b58;
    box-shadow: 0 0 0 2px rgba(200, 155, 88, 0.12);
  }
}

.squad-field {
  position: relative;

  small {
    position: absolute;
    right: 10px;
    bottom: 13px;
    color: var(--color-text-secondary);
    font-size: 10px;
  }

  input { padding-right: 58px; }
}

.squad-cost-note {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 14px;
  color: var(--color-text-secondary);
  background: rgba(183, 126, 59, 0.09);
  border: 1px solid rgba(200, 155, 88, 0.3);
  border-radius: 10px;
  font-size: 12px;

  strong { color: #d6aa66; font-size: 14px; }
}

.squad-btn {
  min-height: 38px;
  padding: 8px 14px;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;

  &:disabled { cursor: not-allowed; opacity: 0.48; }
  &.wide { width: 100%; min-height: 46px; }
  &.primary {
    color: #20170e;
    background: linear-gradient(135deg, #e0b86b, #b97d3f);
    border-color: #e0b86b;
  }
  &.secondary:hover:not(:disabled) { color: #d6aa66; border-color: #c89b58; }
  &.danger-ghost {
    color: #d17070;
    background: rgba(150, 52, 52, 0.08);
    border-color: rgba(196, 78, 78, 0.45);
  }
}

.squad-browse-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.squad-browse-card {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  gap: 12px;
  padding: 14px;
  background: color-mix(in srgb, var(--color-bg) 54%, transparent);
  border: 1px solid var(--color-border);
  border-radius: 12px;

  > .squad-btn { grid-column: 1 / -1; }
}

.squad-browse-copy {
  min-width: 0;

  h4 {
    overflow: hidden;
    margin: 0 0 3px;
    color: var(--color-text);
    font-size: 15px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  p,
  span {
    margin: 0;
    color: var(--color-text-secondary);
    font-size: 11px;
  }
}

.squad-pagination {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  margin-top: 18px;
  color: var(--color-text-secondary);
  font-size: 12px;

  button {
    padding: 6px 12px;
    color: var(--color-text);
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 7px;
    cursor: pointer;
  }

  button:disabled { cursor: not-allowed; opacity: 0.45; }
}

@media (max-width: 720px) {
  .squad-modal-overlay {
    align-items: flex-end;
    padding: 0;
  }

  .squad-modal {
    width: 100%;
    max-height: 94vh;
    max-height: 94dvh;
    border-radius: 18px 18px 0 0;
  }

  .squad-modal-header { padding: 18px 16px 14px; }
  .squad-modal-body {
    max-height: calc(94vh - 104px);
    max-height: calc(94dvh - 104px);
    padding: 16px 16px calc(16px + env(safe-area-inset-bottom));
  }
  .squad-create-layout { grid-template-columns: 1fr; }
  .squad-preview-stage {
    position: static;
    min-height: 0;
    gap: 12px;
    padding: 18px;
  }
  .squad-member-list,
  .squad-browse-grid { grid-template-columns: 1fr; }
  .squad-field input { font-size: 16px; }
  .squad-actions,
  .squad-leave-panel { align-items: stretch; flex-direction: column; }
  .squad-actions .squad-btn,
  .squad-leave-panel .squad-btn { width: 100%; }
}

@media (max-width: 380px) {
  .squad-current-hero { align-items: flex-start; gap: 14px; padding: 14px; }
  .squad-current-copy h3 { font-size: 20px; }
}
</style>
