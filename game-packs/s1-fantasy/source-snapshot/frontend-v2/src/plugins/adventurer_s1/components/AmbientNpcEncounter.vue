<template>
  <section
    v-if="encounter"
    class="ambient-npc"
    :class="[
      `ambient-npc--${variant}`,
      { 'ambient-npc--collapsed': !expanded },
    ]"
    :aria-label="`与${encounter.npc.name}的环境伙伴偶遇`"
  >
    <div class="ambient-npc__glow" aria-hidden="true" />
    <header class="ambient-npc__header">
      <div class="ambient-npc__portrait" aria-hidden="true">{{ encounter.npc.icon }}</div>
      <div class="ambient-npc__identity">
        <span class="ambient-npc__eyebrow">角落里的相遇</span>
        <h3>{{ encounter.npc.name }}</h3>
        <span class="ambient-npc__breed">{{ encounter.npc.breed }}</span>
      </div>
      <button
        type="button"
        class="ambient-npc__toggle"
        :aria-expanded="expanded"
        :aria-controls="detailsId"
        :aria-label="expanded ? `收起与${encounter.npc.name}的互动` : `展开与${encounter.npc.name}的互动`"
        :disabled="busyActionKey !== null"
        @click="toggleExpanded"
      >
        <span>{{ expanded ? '收起' : '展开' }}</span>
        <span class="ambient-npc__toggle-icon" :class="{ 'is-expanded': expanded }" aria-hidden="true">⌄</span>
      </button>
    </header>

    <div v-if="!expanded" class="ambient-npc__summary">
      <p>{{ encounter.npc.presence_text || encounter.npc.short_intro }}</p>
      <span aria-live="polite">{{ collapsedStatus }}</span>
    </div>

    <div v-show="expanded" :id="detailsId" class="ambient-npc__details">
      <div class="ambient-npc__story">
        <p class="ambient-npc__intro">{{ encounter.npc.short_intro }}</p>
        <p v-if="encounter.npc.public_background" class="ambient-npc__background">
          <span>小传</span>{{ encounter.npc.public_background }}
        </p>
        <p class="ambient-npc__presence">{{ encounter.npc.presence_text }}</p>
      </div>

      <div class="ambient-npc__responses" aria-live="polite">
        <blockquote
          v-for="action in completedActions"
          :key="action.key"
          class="ambient-npc__response"
        >
          {{ action.last_text }}
        </blockquote>
      </div>

      <div v-if="pendingInteraction" class="ambient-npc__pending" role="status">
        <strong>{{ pendingInteraction.item ? '上一份赠礼仍在核对' : '上一次互动仍在核对' }}</strong>
        <span v-if="pendingInteraction.item">
          已锁定「{{ pendingInteraction.item.name }}」。结果明确前不能改送其他物品，点击对应互动可沿用原请求核对。
        </span>
        <span v-else>结果明确前会沿用原请求，点击对应互动可继续核对。</span>
      </div>

      <div class="ambient-npc__actions">
        <button
          v-for="action in encounter.actions"
          :key="action.key"
          type="button"
          class="ambient-npc__action"
          :class="{ 'is-complete': action.completed }"
          :disabled="!actionAvailable(action) || busyActionKey !== null"
          @click="chooseAction(action)"
        >
          <span>{{ actionDisplayLabel(action) }}</span>
          <span v-if="busyActionKey === action.key" class="ambient-npc__busy">稍候…</span>
        </button>
      </div>

      <div v-if="pickerAction" class="ambient-npc__picker">
        <div class="ambient-npc__picker-heading">
          <div>
            <strong>{{ pickerPendingAction ? '核对原礼物' : '挑一件礼物' }}</strong>
            <span v-if="pickerPendingAction">
              已锁定原礼物与原请求，结果明确前不能改选
            </span>
            <span v-else>无论对方接受还是拒绝，确认送出后都会永久消耗 1 件</span>
          </div>
          <button type="button" class="ambient-npc__close" aria-label="关闭物品选择" @click="closePicker">×</button>
        </div>

        <p v-if="itemOptionsLoading" class="ambient-npc__muted">正在翻找行囊与仓库…</p>
        <p v-else-if="itemOptionsError" class="ambient-npc__error">{{ itemOptionsError }}</p>
        <p v-else-if="itemOptions.length === 0 && !pickerPendingAction" class="ambient-npc__muted">
          行囊与仓库里暂时没有可以送出的物品。
        </p>
        <template v-else>
          <input
            v-if="!pickerPendingAction"
            v-model="itemSearch"
            type="search"
            class="ambient-npc__search"
            placeholder="搜索名称、类型或描述"
            aria-label="搜索可以赠送的物品"
            autocomplete="off"
          >
          <InventoryListToolbar
            v-if="!pickerPendingAction"
            v-model:rarity="itemRarityFilter"
            v-model:sort="itemSort"
            :items="searchFilteredItemOptions"
            aria-label="按稀有度筛选可以赠送的物品"
          />
          <p v-if="filteredItemOptions.length === 0" class="ambient-npc__muted">
            当前搜索或稀有度筛选下没有可赠送物品。
          </p>
        </template>
        <div v-if="filteredItemOptions.length > 0" class="ambient-npc__items">
          <button
            v-for="item in filteredItemOptions"
            :key="`${item.source}:${item.id}`"
            type="button"
            class="ambient-npc__item"
            :class="`risk-${item.confirmation_level}`"
            :disabled="busyActionKey !== null || item.is_player_locked"
            @click="prepareGiftConfirmation(item)"
          >
            <span class="ambient-npc__item-heading">
              <InventorySelectionItemSummary
                :name="item.name"
                :rarity="item.rarity"
                :quantity="item.quantity"
                :meta="[item.source === 'carry' ? '随身' : '仓库', itemTypeLabel(item.type), ...(item.is_player_locked ? ['已保护'] : [])]"
                :state="giftItemStateSummary(item)"
                :description="item.description"
              />
              <span
                v-if="item.confirmation_level !== 'normal'"
                class="ambient-npc__risk-badge"
              >
                {{ confirmationLevelLabel(item.confirmation_level) }}
              </span>
            </span>
          </button>
        </div>
      </div>

      <p v-if="actionError" class="ambient-npc__error" role="alert">{{ actionError }}</p>
    </div>
  </section>

  <S1ConfirmDialog
    v-if="confirmationAction && confirmationItem"
    :key="`${confirmationItem.source}:${confirmationItem.id}`"
    :title="giftConfirmationTitle"
    :message="giftConfirmationMessage"
    :confirm-label="giftConfirmationLabel"
    cancel-label="返回挑选"
    :busy="busyActionKey === confirmationAction.key"
    :required-phrase="giftRequiredPhrase"
    @confirm="confirmGift"
    @cancel="closeGiftConfirmation"
  />
</template>

<script setup lang="ts">
import { computed, ref, toRef, useId, watch } from 'vue'
import {
  AmbientNpcTerminalActionError,
  useAmbientNpcEncounter,
  type AmbientNpcActionView,
  type AmbientNpcConfirmationLevel,
  type AmbientNpcItemOption,
  type AmbientNpcPendingAction,
} from '../composables/useAmbientNpcEncounter'
import { useInventoryListControls } from '../composables/useInventoryListControls'
import { itemTypeLabel } from '../utils'
import InventoryListToolbar from './InventoryListToolbar.vue'
import InventorySelectionItemSummary from './InventorySelectionItemSummary.vue'
import S1ConfirmDialog from './S1ConfirmDialog.vue'

const props = withDefaults(defineProps<{
  surface: string | null
  variant?: 'strip' | 'embedded'
}>(), {
  variant: 'strip',
})

const {
  encounter,
  busyActionKey,
  itemOptions,
  itemOptionsLoading,
  itemOptionsError,
  pendingActions,
  loadItems,
  performAction,
} = useAmbientNpcEncounter(toRef(props, 'surface'))

const pickerAction = ref<AmbientNpcActionView | null>(null)
const actionError = ref('')
const itemSearch = ref('')
const confirmationAction = ref<AmbientNpcActionView | null>(null)
const confirmationItem = ref<AmbientNpcItemOption | null>(null)
const confirmationError = ref('')
const expanded = ref(false)
const detailsId = `ambient-npc-details-${useId()}`
let submitSequence = 0
const completedActions = computed(() => (
  encounter.value?.actions.filter(action => action.completed && action.last_text) || []
))
const pendingInteraction = computed<AmbientNpcPendingAction | null>(() => (
  pendingActions.value[0] || null
))
const pickerPendingAction = computed(() => (
  pickerAction.value ? pendingActionFor(pickerAction.value.key) : null
))
const confirmationPendingAction = computed(() => (
  confirmationAction.value ? pendingActionFor(confirmationAction.value.key) : null
))
const collapsedStatus = computed(() => {
  const actions = encounter.value?.actions || []
  if (busyActionKey.value !== null) return '正在回应…'
  if (pendingInteraction.value) return '上一次互动结果待确认'
  if (actions.length === 0) return '它只是安静地待在这里'
  const availableCount = actions.filter(actionAvailable).length
  if (availableCount > 0) {
    return actions.some(action => action.completed)
      ? '还有互动可以进行'
      : '可以靠近看看'
  }
  return actions.some(action => action.completed)
    ? '今天已经回应过了'
    : '现在不便打扰'
})
const pickerItemOptions = computed(() => {
  const pendingItem = pickerPendingAction.value?.item
  return pendingItem ? [pendingItem] : itemOptions.value
})
const searchFilteredItemOptions = computed(() => {
  const options = pickerItemOptions.value
  const query = itemSearch.value.trim().toLocaleLowerCase()
  if (!query || pickerPendingAction.value) return options
  return options.filter((item) => (
    [
      item.name,
      item.type,
      item.description,
      item.source === 'carry' ? '随身' : '仓库',
    ]
      .join('\n')
      .toLocaleLowerCase()
      .includes(query)
  ))
})
const {
  rarityFilter: itemRarityFilter,
  sort: itemSort,
  visibleItems: controlledItemOptions,
} = useInventoryListControls(searchFilteredItemOptions)
const filteredItemOptions = computed(() => {
  const pendingItem = pickerPendingAction.value?.item
  return pendingItem ? [pendingItem] : controlledItemOptions.value
})

const giftConfirmationTitle = computed(() => {
  if (confirmationItem.value?.confirmation_level === 'irreversible') {
    return '确认送出不可替代的礼物？'
  }
  if (confirmationItem.value?.confirmation_level === 'valuable') {
    return '确认送出贵重礼物？'
  }
  return '送出这份礼物？'
})

const giftConfirmationMessage = computed(() => {
  const item = confirmationItem.value
  if (!item) return ''
  const pending = confirmationPendingAction.value
  const lines = pending
    ? [
        `上一次已尝试从${item.source === 'carry' ? '随身物品' : '仓库'}送出「${item.name}」。`,
        '结果尚未确认。本次只会沿用原礼物与原请求核对，不会创建另一笔赠礼。',
      ]
    : [
        `你将从${item.source === 'carry' ? '随身物品' : '仓库'}中送出「${item.name}」1 件，当前共有 ${item.quantity} 件。`,
      ]
  lines.push('', item.warning_text || '无论对方接受还是拒绝，赠出后都会永久消耗 1 件物品，且无法撤回。')
  if (item.description) lines.push('', `物品描述：${item.description}`)
  if (confirmationError.value) {
    lines.push(
      '',
      confirmationError.value,
      '若刚才等待超时，请不要改选其他物品；再次确认会沿用刚才那次请求核对结果。',
    )
  }
  return lines.join('\n')
})

const giftConfirmationLabel = computed(() => (
  confirmationPendingAction.value || confirmationError.value
    ? '重试并核对结果'
    : '送出并永久消耗 1 件'
))

const giftRequiredPhrase = computed(() => (
  confirmationItem.value?.confirmation_level === 'irreversible'
    ? confirmationItem.value.name
    : ''
))

function pendingActionFor(actionKey: string): AmbientNpcPendingAction | null {
  return pendingActions.value.find(pending => pending.actionKey === actionKey) || null
}

function actionAvailable(action: AmbientNpcActionView): boolean {
  return pendingActionFor(action.key) !== null
    || (!action.completed && action.available !== false)
}

function actionDisplayLabel(action: AmbientNpcActionView): string {
  const pending = pendingActionFor(action.key)
  if (pending?.item) return `核对「${pending.item.name}」的赠礼结果`
  if (pending) return '核对上一次互动结果'
  if (actionAvailable(action)) return action.label
  if (action.unavailable_reason) return action.unavailable_reason
  return action.requires_item ? '今天已经赠过礼物' : '今天已经互动过了'
}

function confirmationLevelLabel(level: AmbientNpcConfirmationLevel): string {
  if (level === 'irreversible') return '不可逆高风险'
  if (level === 'valuable') return '贵重物品'
  return ''
}

function giftItemStateSummary(item: AmbientNpcItemOption): string {
  const details: string[] = []
  if (item.is_player_locked) details.push('已保护，需先解锁')
  if (item.evolution_stage != null) details.push(`进化阶段 ${item.evolution_stage}`)
  if (item.uses_left != null) details.push(`剩余使用次数 ${item.uses_left}`)
  return details.join(' · ')
}

function clearTransientInteractionUi() {
  pickerAction.value = null
  itemSearch.value = ''
  confirmationAction.value = null
  confirmationItem.value = null
  confirmationError.value = ''
  actionError.value = ''
}

function toggleExpanded() {
  if (busyActionKey.value !== null) return
  expanded.value = !expanded.value
  if (!expanded.value) clearTransientInteractionUi()
}

function closePicker() {
  pickerAction.value = null
  itemSearch.value = ''
  closeGiftConfirmation()
  actionError.value = ''
}

async function chooseAction(action: AmbientNpcActionView) {
  if (!actionAvailable(action)) return
  actionError.value = ''
  const pending = pendingActionFor(action.key)
  if (pending?.item) {
    pickerAction.value = action
    itemSearch.value = ''
    confirmationAction.value = action
    confirmationItem.value = { ...pending.item }
    confirmationError.value = '上一次赠礼的结果尚未确认，只能沿用这件礼物和原请求继续核对。'
    return
  }
  if (pending) {
    await submitAction(action)
    return
  }
  if (action.requires_item) {
    pickerAction.value = action
    itemSearch.value = ''
    await loadItems(action)
    return
  }
  await submitAction(action)
}

function prepareGiftConfirmation(item: AmbientNpcItemOption) {
  const action = pickerAction.value
  if (!action) return
  if (busyActionKey.value !== null || !actionAvailable(action) || item.is_player_locked) return
  const pending = pendingActionFor(action.key)
  if (pending?.item) {
    confirmationAction.value = action
    confirmationItem.value = { ...pending.item }
    confirmationError.value = '原请求仍在核对，不能改送其他物品。'
    actionError.value = ''
    return
  }
  confirmationAction.value = action
  confirmationItem.value = item
  confirmationError.value = ''
  actionError.value = ''
}

function closeGiftConfirmation() {
  if (busyActionKey.value !== null) return
  confirmationAction.value = null
  confirmationItem.value = null
  confirmationError.value = ''
}

async function confirmGift() {
  const action = confirmationAction.value
  const item = confirmationItem.value
  if (!action || !item) return
  await submitAction(action, item)
}

async function submitAction(action: AmbientNpcActionView, item?: AmbientNpcItemOption) {
  const sequence = ++submitSequence
  actionError.value = ''
  try {
    const result = await performAction(action, item)
    if (sequence !== submitSequence) return
    if (result) {
      pickerAction.value = null
      itemSearch.value = ''
      closeGiftConfirmation()
    }
  } catch (error: unknown) {
    if (sequence !== submitSequence) return
    if (error instanceof AmbientNpcTerminalActionError) {
      const message = error.message || '这次互动未能完成，请稍后重新尝试'
      clearTransientInteractionUi()
      actionError.value = message
      return
    }
    const typed = error as {
      message?: string
      response?: { data?: { message?: string } }
    }
    const message = typed?.response?.data?.message
      || typed?.message
      || '这次互动的结果暂时无法确认，请沿用当前礼物重试'
    actionError.value = message
    const pending = pendingActionFor(action.key)
    if (pending?.item) {
      pickerAction.value = action
      confirmationAction.value = action
      confirmationItem.value = { ...pending.item }
      confirmationError.value = message
    } else if (
      item
      && confirmationItem.value?.source === item.source
      && confirmationItem.value?.id === item.id
    ) {
      confirmationError.value = message
    }
  }
}

watch(pendingActions, () => {
  const selectedAction = confirmationAction.value || pickerAction.value
  if (!selectedAction) return
  const pending = pendingActionFor(selectedAction.key)
  if (!pending?.item) return
  pickerAction.value = selectedAction
  confirmationAction.value = selectedAction
  confirmationItem.value = { ...pending.item }
  confirmationError.value = '这份赠礼正在核对，当前只能沿用原礼物与原请求。'
})

watch(
  () => encounter.value?.id,
  () => {
    submitSequence += 1
    expanded.value = false
    clearTransientInteractionUi()
  },
)

watch(
  () => {
    const selectedAction = confirmationAction.value || pickerAction.value
    if (!selectedAction) return true
    const currentAction = encounter.value?.actions.find(action => action.key === selectedAction.key)
    return currentAction ? actionAvailable(currentAction) : false
  },
  (available) => {
    if (available) return
    // 同一偶遇可能在另一个 surface 先完成，不保留已过期的物品选择与确认框。
    clearTransientInteractionUi()
  },
)
</script>

<style scoped>
.ambient-npc {
  position: relative;
  overflow: hidden;
  margin: 14px 0 18px;
  padding: 17px 18px 16px;
  border: 1px solid rgba(202, 158, 82, 0.38);
  border-radius: 14px;
  background:
    linear-gradient(115deg, rgba(37, 29, 25, 0.97), rgba(24, 25, 31, 0.96)),
    #211d1b;
  color: #eee5d6;
  box-shadow: 0 12px 30px rgba(7, 6, 8, 0.2), inset 0 1px rgba(255, 244, 218, 0.04);
}

.ambient-npc--embedded {
  margin-inline: 0;
  border-radius: 10px;
}

.ambient-npc--collapsed {
  padding: 13px 15px 12px;
  box-shadow: 0 7px 18px rgba(7, 6, 8, 0.16), inset 0 1px rgba(255, 244, 218, 0.04);
}

.ambient-npc--collapsed .ambient-npc__portrait {
  width: 44px;
  height: 44px;
  flex-basis: 44px;
  font-size: 24px;
}

.ambient-npc__glow {
  position: absolute;
  width: 180px;
  height: 180px;
  right: -65px;
  top: -95px;
  border-radius: 50%;
  background: rgba(205, 145, 61, 0.13);
  filter: blur(22px);
  pointer-events: none;
}

.ambient-npc__header {
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
}

.ambient-npc__toggle {
  display: inline-flex;
  min-width: 68px;
  min-height: 44px;
  margin-left: auto;
  padding: 8px 10px;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  gap: 5px;
  border: 1px solid rgba(209, 166, 89, 0.3);
  border-radius: 999px;
  background: rgba(7, 7, 10, 0.28);
  color: #d9c6a7;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  transition: border-color 0.16s ease, background 0.16s ease;
}

.ambient-npc__toggle:not(:disabled):hover {
  border-color: rgba(234, 190, 110, 0.7);
  background: rgba(84, 61, 36, 0.38);
}

.ambient-npc__toggle:disabled {
  cursor: default;
  opacity: 0.58;
}

.ambient-npc__toggle:focus-visible {
  outline: 2px solid rgba(242, 199, 119, 0.9);
  outline-offset: 2px;
}

.ambient-npc__toggle-icon {
  display: inline-block;
  font-size: 17px;
  line-height: 1;
  transform: translateY(-1px);
  transition: transform 0.16s ease;
}

.ambient-npc__toggle-icon.is-expanded {
  transform: translateY(1px) rotate(180deg);
}

.ambient-npc__portrait {
  display: grid;
  place-items: center;
  width: 52px;
  height: 52px;
  flex: 0 0 52px;
  border: 1px solid rgba(229, 184, 101, 0.4);
  border-radius: 50%;
  background: rgba(7, 7, 10, 0.45);
  font-size: 28px;
  box-shadow: inset 0 0 18px rgba(237, 185, 88, 0.08);
}

.ambient-npc__identity {
  min-width: 0;
  display: grid;
  grid-template-columns: auto 1fr;
  column-gap: 9px;
  align-items: baseline;
}

.ambient-npc__eyebrow {
  grid-column: 1 / -1;
  margin-bottom: 2px;
  color: #c9a96d;
  font-size: 11px;
  letter-spacing: 0.16em;
}

.ambient-npc h3 {
  margin: 0;
  color: #fff4dc;
  font-size: 19px;
  font-weight: 650;
}

.ambient-npc__breed {
  color: #a99f92;
  font-size: 12px;
}

.ambient-npc__summary {
  position: relative;
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 10px;
  margin: 8px 0 0 56px;
}

.ambient-npc__summary p {
  display: -webkit-box;
  min-width: 0;
  flex: 1 1 auto;
  margin: 0;
  overflow: hidden;
  color: #d8cdbd;
  font-size: 13px;
  line-height: 1.55;
  overflow-wrap: anywhere;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 1;
}

.ambient-npc__summary span {
  flex: 0 0 auto;
  color: #b89a67;
  font-size: 11px;
  white-space: nowrap;
}

.ambient-npc__story {
  position: relative;
  margin-top: 13px;
  padding-left: 12px;
  border-left: 2px solid rgba(210, 164, 86, 0.32);
}

.ambient-npc__story p {
  margin: 0;
  line-height: 1.75;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.ambient-npc__intro {
  color: #c8beb0;
  font-size: 13px;
}

.ambient-npc__background {
  margin-top: 7px !important;
  color: #d8cdbd;
  font-size: 13px;
}

.ambient-npc__background span {
  display: inline-block;
  margin-right: 7px;
  color: #b89a67;
  font-size: 11px;
  letter-spacing: 0.08em;
}

.ambient-npc__presence {
  margin-top: 6px !important;
  color: #f0e6d5;
  font-size: 14px;
}

.ambient-npc__responses {
  display: grid;
  gap: 7px;
  margin-top: 12px;
}

.ambient-npc__pending {
  display: grid;
  gap: 3px;
  margin-top: 12px;
  padding: 9px 11px;
  border: 1px solid rgba(224, 161, 77, 0.5);
  border-radius: 8px;
  background: rgba(105, 69, 34, 0.25);
  color: #d9cbb5;
  font-size: 12px;
  line-height: 1.6;
}

.ambient-npc__pending strong {
  color: #f0d6a8;
  font-size: 13px;
}

.ambient-npc__response {
  margin: 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: rgba(235, 198, 130, 0.075);
  color: #e8d9c2;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.ambient-npc__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 9px;
  margin-top: 14px;
}

.ambient-npc__action,
.ambient-npc__item {
  min-height: 44px;
  border: 1px solid rgba(209, 166, 89, 0.45);
  border-radius: 9px;
  background: rgba(84, 61, 36, 0.38);
  color: #f1dfbf;
  cursor: pointer;
  transition: border-color 0.16s ease, background 0.16s ease, transform 0.16s ease;
}

.ambient-npc__action {
  min-width: 150px;
  padding: 9px 14px;
}

.ambient-npc__action:not(:disabled):hover,
.ambient-npc__item:not(:disabled):hover {
  border-color: rgba(234, 190, 110, 0.8);
  background: rgba(115, 80, 40, 0.52);
  transform: translateY(-1px);
}

.ambient-npc__action:disabled,
.ambient-npc__item:disabled {
  cursor: default;
  opacity: 0.58;
}

.ambient-npc__action.is-complete {
  border-color: rgba(141, 136, 125, 0.24);
  background: rgba(70, 67, 64, 0.25);
}

.ambient-npc__busy {
  display: block;
  margin-top: 2px;
  color: #bdac91;
  font-size: 11px;
}

.ambient-npc__picker {
  margin-top: 13px;
  padding: 12px;
  border: 1px solid rgba(185, 148, 85, 0.26);
  border-radius: 10px;
  background: rgba(9, 9, 12, 0.32);
}

.ambient-npc__picker-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}

.ambient-npc__picker-heading div {
  display: grid;
  gap: 2px;
}

.ambient-npc__picker-heading strong {
  color: #f2e2c6;
  font-size: 14px;
}

.ambient-npc__picker-heading span,
.ambient-npc__muted {
  color: #a99f92;
  font-size: 12px;
}

.ambient-npc__close {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  margin: -8px -8px 0 0;
  border: 0;
  background: transparent;
  color: #c8bba6;
  font-size: 24px;
  cursor: pointer;
}

.ambient-npc__search {
  width: 100%;
  min-height: 44px;
  box-sizing: border-box;
  margin-top: 11px;
  padding: 9px 11px;
  border: 1px solid rgba(185, 148, 85, 0.32);
  border-radius: 8px;
  outline: none;
  background: rgba(5, 6, 9, 0.5);
  color: #eee5d6;
  font: inherit;
}

.ambient-npc__search::placeholder {
  color: #8f867a;
}

.ambient-npc__search:focus {
  border-color: rgba(234, 190, 110, 0.75);
  box-shadow: 0 0 0 2px rgba(211, 165, 84, 0.13);
}

.ambient-npc__items {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px;
  margin-top: 10px;
}

.ambient-npc__item {
  display: grid;
  min-width: 0;
  justify-items: start;
  gap: 4px;
  padding: 9px 11px;
  text-align: left;
}

.ambient-npc__item.risk-valuable {
  border-color: rgba(224, 161, 77, 0.68);
}

.ambient-npc__item.risk-irreversible {
  border-color: rgba(218, 105, 91, 0.78);
  background: rgba(104, 48, 39, 0.3);
}

.ambient-npc__item-heading {
  display: flex;
  width: 100%;
  min-width: 0;
  align-items: flex-start;
  justify-content: space-between;
  gap: 7px;
}

.ambient-npc__risk-badge {
  flex: none;
  padding: 2px 5px;
  border: 1px solid currentColor;
  border-radius: 999px;
  color: #e4a66f;
  font-size: 10px;
  line-height: 1.35;
}

.risk-irreversible .ambient-npc__risk-badge {
  color: #ee9a8d;
}

.ambient-npc__error,
.ambient-npc__muted {
  margin: 10px 0 0;
  line-height: 1.6;
}

.ambient-npc__error {
  color: #e7a29a;
  font-size: 12px;
}

@media (max-width: 640px) {
  .ambient-npc {
    margin: 10px 0 14px;
    padding: 14px;
    border-radius: 11px;
  }

  .ambient-npc--collapsed {
    padding: 12px 13px 11px;
  }

  .ambient-npc__toggle {
    min-width: 64px;
    padding-inline: 8px;
  }

  .ambient-npc__actions {
    display: grid;
    grid-template-columns: 1fr;
  }

  .ambient-npc__action {
    width: 100%;
  }

  .ambient-npc__items {
    grid-template-columns: 1fr;
    max-height: min(44dvh, 360px);
    overflow-y: auto;
    overscroll-behavior: contain;
    padding-bottom: env(safe-area-inset-bottom, 0);
  }

  .ambient-npc--embedded .ambient-npc__items {
    max-height: none;
    overflow-y: visible;
  }
}

@media (prefers-reduced-motion: reduce) {
  .ambient-npc__action,
  .ambient-npc__item,
  .ambient-npc__toggle,
  .ambient-npc__toggle-icon {
    transition: none;
  }
}
</style>
