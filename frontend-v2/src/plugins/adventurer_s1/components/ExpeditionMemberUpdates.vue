<template>
  <div v-if="updates.length" class="member-updates">
    <h4 class="mu-title">⚔️ 成员状态更新</h4>
    <div
      v-for="(mu, idx) in updates"
      :key="mu.id ?? idx"
      class="mu-item"
      :class="{ 'is-comment-active': activeEmptyCommentIndex === idx }"
      :tabindex="readonly || commentCount(idx) ? undefined : 0"
      @click="revealEmptyComment(idx)"
      @keydown.enter.prevent="revealEmptyComment(idx)"
    >
      <div class="mu-head">
        <button
          v-if="mu.id != null && !readonly"
          type="button"
          class="mu-name-link"
          @click.stop="openAdventurer(mu)"
        >
          {{ displayName(mu) }}
        </button>
        <strong v-else>{{ displayName(mu) }}</strong>
        <span v-if="mu.is_dead" class="mu-badge dead">已阵亡</span>
        <span v-else-if="hpChange(mu) < 0" class="mu-badge hurt">受伤</span>
        <span v-else class="mu-badge healthy">健康</span>
        <span v-if="memberScore(mu) != null" class="mu-badge score">
          🏅 {{ memberScore(mu)!.toFixed(1) }}
        </span>
      </div>
      <div class="mu-tags">
        <span v-for="(transfer, transferIndex) in mu.asset_transfers || []" :key="`transfer-${transferIndex}`" class="mu-tag skill">
          {{ transfer.direction === 'received' ? '接收' : '转出' }}{{ transfer.kind === 'skill' ? '技能' : '特质' }}：{{ transfer.name }}
        </span>
        <span v-if="hpChange(mu) !== 0" :class="['mu-tag', hpChange(mu) > 0 ? 'pos' : 'neg']">
          HP{{ hpChange(mu) > 0 ? '+' : '' }}{{ hpChange(mu) }}
        </span>
        <span v-if="mu.is_dead && mu.death_reason" class="mu-tag danger">
          ☠️ {{ mu.death_reason }}
        </span>
        <span v-if="goldChange(mu) !== 0" :class="['mu-tag', goldChange(mu) > 0 ? 'gold' : 'neg']">
          💰{{ goldChange(mu) > 0 ? '+' : '' }}{{ goldChange(mu).toFixed(0) }}
        </span>
        <span v-if="mu.new_class" class="mu-tag class">🎭 职业 → {{ mu.new_class }}</span>
        <span v-if="newOnlyTraitNames(mu).length" class="mu-tag trait">
          ✨ 新特质: {{ newOnlyTraitNames(mu).join('、') }}
        </span>
        <span v-if="modifiedTraitNames(mu).length" class="mu-tag trait">
          🔄 修改特质: {{ modifiedTraitNames(mu).join('、') }}
        </span>
        <span v-if="removedOnlyTraitNames(mu).length" class="mu-tag muted">
          ❌ 移除特质: {{ removedOnlyTraitNames(mu).join('、') }}
        </span>
        <span v-if="newSkillNames(mu).length" class="mu-tag skill">
          📘 新技能: {{ newSkillNames(mu).join('、') }}
        </span>
        <span v-if="modifiedSkillNames(mu).length" class="mu-tag skill">
          🔄 修改技能: {{ modifiedSkillNames(mu).join('、') }}
        </span>
        <span v-if="removedSkillNames(mu).length" class="mu-tag muted">
          ❌ 移除技能: {{ removedSkillNames(mu).join('、') }}
        </span>
        <span v-if="newEquipmentItems(mu).length" class="mu-tag equip mu-tag-equip-list">
          <span class="equip-label">🛡️ 新装备:</span>
          <template v-for="(item, i) in newEquipmentItems(mu)" :key="item.name">
            <span v-if="i > 0" class="equip-sep">、</span>
            <button
              type="button"
              class="equip-preview"
              :style="{ color: rarityColor(item.rarity) }"
              :aria-label="`查看${item.name}的描述`"
              @click.stop="previewEquipment = item"
              @keydown.enter.stop
              @keydown.space.stop
            >{{ item.name }}</button>
            <span class="equip-quantity">×{{ item.quantity }}</span>
            <span
              class="rarity-pill"
              :style="{ color: rarityColor(item.rarity), borderColor: rarityColor(item.rarity) }"
            >{{ rarityLabel(item.rarity) }}</span>
          </template>
        </span>
        <span v-if="modifiedEquipmentNames(mu).length" class="mu-tag equip">
          🔄 修改装备: {{ modifiedEquipmentNames(mu).join('、') }}
        </span>
        <span v-if="discardedEquipmentNames(mu).length" class="mu-tag danger">
          ⚠️ 仓库已满，未能存入并已丢弃: {{ discardedEquipmentNames(mu).join('、') }}
        </span>
        <span v-if="consumedEquipmentNames(mu).length" class="mu-tag danger">
          💔 消耗装备: {{ consumedEquipmentNames(mu).join('、') }}
        </span>
        <span v-if="statIncreases(mu).length" class="mu-tag stat">
          📈 {{ statIncreases(mu).join(' ') }}
        </span>
      </div>
      <button
        v-if="!readonly"
        type="button"
        class="mu-comment-btn"
        :class="{ 'is-empty': !commentCount(idx) }"
        :aria-label="commentCount(idx) ? `查看 ${commentCount(idx)} 条评论` : '评论这条成员状态更新'"
        @click.stop="openComment(idx)"
      >
        <span aria-hidden="true">💬</span>
        <span v-if="commentCount(idx)">{{ commentCount(idx) }}</span>
        <span v-else>评论</span>
      </button>
    </div>
  </div>
  <RewardItemPreviewModal
    :item="previewEquipment"
    kicker="探险所得物品"
    @close="previewEquipment = null"
  />
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { rarityColor, rarityLabel } from '../utils'
import RewardItemPreviewModal from './RewardItemPreviewModal.vue'

export interface MemberUpdateEntry {
  asset_transfers?: Array<{ name: string; kind: 'skill' | 'trait'; direction: 'received' | 'sent' }>
  id?: number
  name?: string
  is_dead?: boolean
  death_reason?: string
  hp_change?: number
  remaining_hp?: number
  gold_change?: number
  score?: number | string | null
  new_class?: string
  new_traits?: unknown
  removed_traits?: unknown
  new_skills?: unknown
  removed_skills?: unknown
  new_equipments?: Record<string, unknown>
  discarded_equipments?: unknown[]
  consumed_equipments?: unknown[]
  strength_increase?: number
  agility_increase?: number
  intelligence_increase?: number
  luck_increase?: number
  max_hp_increase?: number
  modified_traits?: unknown
  modified_skills?: unknown
  modified_equipments?: unknown
}

const props = defineProps<{
  memberUpdates?: MemberUpdateEntry[] | null
  commentCounts?: Record<string, number> | null
  readonly?: boolean
}>()
const emit = defineEmits<{
  open: [payload: { id: number; name?: string }]
  comment: [index: number]
}>()
const activeEmptyCommentIndex = ref<number | null>(null)
const previewEquipment = ref<NewEquipmentItem | null>(null)

function displayName(mu: MemberUpdateEntry): string {
  return mu.name || `冒险者 #${mu.id}`
}

function openAdventurer(mu: MemberUpdateEntry) {
  if (props.readonly || mu.id == null) return
  emit('open', { id: mu.id, name: mu.name })
}

const updates = computed(() =>
  Array.isArray(props.memberUpdates) ? props.memberUpdates : [],
)

function commentCount(index: number): number {
  const count = Number(props.commentCounts?.[String(index)] ?? 0)
  return Number.isFinite(count) && count > 0 ? count : 0
}

function revealEmptyComment(index: number) {
  if (props.readonly || commentCount(index)) return
  activeEmptyCommentIndex.value = index
}

function openComment(index: number) {
  if (props.readonly) return
  activeEmptyCommentIndex.value = null
  emit('comment', index)
}

function stripName(raw: string): string {
  return raw.replace(/\s*[（(].*[）)]\s*$/, '').trim()
}

function extractNameList(source: unknown): string[] {
  if (!source) return []
  if (Array.isArray(source)) {
    return source
      .map((item) => {
        if (typeof item === 'string') return stripName(item)
        if (item && typeof item === 'object' && typeof (item as { name?: string }).name === 'string') {
          return stripName((item as { name: string }).name)
        }
        return ''
      })
      .filter(Boolean)
  }
  if (typeof source === 'object') {
    return Object.keys(source as Record<string, unknown>).map(stripName).filter(Boolean)
  }
  return []
}

function hpChange(mu: MemberUpdateEntry): number {
  if (Number.isFinite(Number(mu.hp_change))) return Number(mu.hp_change)
  return 0
}

function goldChange(mu: MemberUpdateEntry): number {
  return Number.isFinite(Number(mu.gold_change)) ? Number(mu.gold_change) : 0
}

function memberScore(mu: MemberUpdateEntry): number | null {
  if (mu.score == null || mu.score === '') return null
  const n = Number(mu.score)
  return Number.isFinite(n) ? n : null
}

function newTraitNames(mu: MemberUpdateEntry): string[] {
  return extractNameList(mu.new_traits)
}

function removedTraitNames(mu: MemberUpdateEntry): string[] {
  return extractNameList(mu.removed_traits)
}

function inferredModifiedTraitNames(mu: MemberUpdateEntry): string[] {
  const added = new Set(newTraitNames(mu))
  return removedTraitNames(mu).filter((name) => added.has(name))
}

function modifiedTraitNames(mu: MemberUpdateEntry): string[] {
  return Array.from(new Set([
    ...extractNameList(mu.modified_traits),
    ...inferredModifiedTraitNames(mu),
  ]))
}

function removedOnlyTraitNames(mu: MemberUpdateEntry): string[] {
  const modified = new Set(modifiedTraitNames(mu))
  return removedTraitNames(mu).filter((name) => !modified.has(name))
}

function newOnlyTraitNames(mu: MemberUpdateEntry): string[] {
  const modified = new Set(modifiedTraitNames(mu))
  return newTraitNames(mu).filter((name) => !modified.has(name))
}

function newSkillNames(mu: MemberUpdateEntry): string[] {
  return extractNameList(mu.new_skills)
}

function removedSkillNames(mu: MemberUpdateEntry): string[] {
  return extractNameList(mu.removed_skills)
}

function modifiedSkillNames(mu: MemberUpdateEntry): string[] {
  return extractNameList(mu.modified_skills)
}

function modifiedEquipmentNames(mu: MemberUpdateEntry): string[] {
  return extractNameList(mu.modified_equipments)
}

interface NewEquipmentItem {
  name: string
  rarity?: string
  quantity: number
  desc?: string
}

function newEquipmentItems(mu: MemberUpdateEntry): NewEquipmentItem[] {
  if (!mu.new_equipments || typeof mu.new_equipments !== 'object') return []
  return Object.entries(mu.new_equipments)
    .flatMap(([rawName, data]): NewEquipmentItem[] => {
      const name = rawName.trim()
      if (!name) return []
      const rarity =
        data && typeof data === 'object' && typeof (data as { rarity?: string }).rarity === 'string'
          ? (data as { rarity: string }).rarity
          : undefined
      const rawQuantity =
        data && typeof data === 'object' ? Number((data as { quantity?: unknown }).quantity ?? 1) : 1
      const quantity = Number.isInteger(rawQuantity) && rawQuantity > 0 ? rawQuantity : 1
      const fields = data && typeof data === 'object' ? data as Record<string, unknown> : {}
      const desc = typeof fields.description === 'string' && fields.description
        ? fields.description : typeof fields.desc === 'string' ? fields.desc : undefined
      return [{ name, rarity, quantity, desc }]
    })
}

function discardedEquipmentNames(mu: MemberUpdateEntry): string[] {
  if (!Array.isArray(mu.discarded_equipments)) return []
  return mu.discarded_equipments
    .map((item) => {
      if (!item || typeof item !== 'object') return ''
      const rawName = (item as { name?: unknown }).name
      if (typeof rawName !== 'string') return ''
      const name = stripName(rawName)
      if (!name) return ''
      const rawQuantity = Number((item as { quantity?: unknown }).quantity ?? 1)
      const quantity = Number.isInteger(rawQuantity) && rawQuantity > 0 ? rawQuantity : 1
      return quantity > 1 ? `${name} ×${quantity}` : name
    })
    .filter(Boolean)
}

function consumedEquipmentNames(mu: MemberUpdateEntry): string[] {
  if (!Array.isArray(mu.consumed_equipments)) return []
  const quantities = new Map<string, number>()
  for (const item of mu.consumed_equipments) {
    const rawName =
      typeof item === 'string'
        ? item
        : item && typeof item === 'object' && typeof (item as { name?: string }).name === 'string'
          ? (item as { name: string }).name
          : ''
    const name = stripName(rawName)
    if (!name) continue
    const rawQuantity =
      item && typeof item === 'object' ? Number((item as { quantity?: unknown }).quantity ?? 1) : 1
    const quantity = Number.isInteger(rawQuantity) && rawQuantity > 0 ? rawQuantity : 1
    quantities.set(name, (quantities.get(name) ?? 0) + quantity)
  }
  return Array.from(quantities, ([name, quantity]) =>
    quantity > 1 ? `${name} ×${quantity}` : name,
  )
}

function statIncreases(mu: MemberUpdateEntry): string[] {
  const attrs: string[] = []
  if (mu.strength_increase) attrs.push(`力+${mu.strength_increase}`)
  if (mu.agility_increase) attrs.push(`敏+${mu.agility_increase}`)
  if (mu.intelligence_increase) attrs.push(`智+${mu.intelligence_increase}`)
  if (mu.luck_increase) attrs.push(`运+${mu.luck_increase}`)
  if (mu.max_hp_increase) attrs.push(`血上限+${mu.max_hp_increase}`)
  return attrs
}

</script>

<style scoped lang="scss">
.equip-preview {
  min-height: 44px;
  padding: 4px 0;
  border: 0;
  background: transparent;
  font: inherit;
  font-weight: 700;
  text-decoration: underline dotted;
  text-underline-offset: 4px;
  text-align: left;
  cursor: pointer;
}
.member-updates {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--color-border);
}

.mu-title {
  margin: 0 0 10px;
  font-size: 14px;
  color: var(--color-text);
}

.mu-item {
  position: relative;
  padding: 10px 12px;
  margin-bottom: 8px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;

  &:focus-visible {
    outline: 1px solid color-mix(in srgb, var(--color-primary) 45%, transparent);
    outline-offset: 2px;
  }
}

.mu-comment-btn {
  display: flex;
  align-items: center;
  gap: 3px;
  min-width: 26px;
  height: 24px;
  margin-top: 8px;
  margin-left: auto;
  width: fit-content;
  padding: 0 7px;
  border: 0;
  border-radius: 999px;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 11px;
  cursor: pointer;

  &:hover,
  &:focus-visible {
    color: var(--color-primary);
    background: color-mix(in srgb, var(--color-primary) 10%, transparent);
  }
}

.mu-comment-btn.is-empty {
  display: none;
  color: var(--color-primary);
}

.mu-item.is-comment-active .mu-comment-btn.is-empty,
.mu-item:focus-within .mu-comment-btn.is-empty {
  display: flex;
}

@media (hover: hover) and (pointer: fine) {
  .mu-item:hover .mu-comment-btn.is-empty {
    display: flex;
  }
}

.mu-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
  font-size: 13px;
  color: var(--color-text);
}

.mu-name-link {
  border: none;
  padding: 0;
  margin: 0;
  font: inherit;
  font-weight: 700;
  color: var(--color-primary);
  background: transparent;
  cursor: pointer;
  text-align: left;

  &:hover {
    text-decoration: underline;
  }
}

.mu-badge {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 8px;

  &.healthy {
    color: var(--color-success);
    background: color-mix(in srgb, var(--color-success) 12%, transparent);
  }

  &.hurt {
    color: var(--color-danger);
    background: color-mix(in srgb, var(--color-danger) 12%, transparent);
  }

  &.dead {
    color: #fff;
    background: var(--color-danger);
  }

  &.score {
    color: #f0883e;
    background: rgba(240, 136, 62, 0.14);
  }
}

.mu-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.mu-tag {
  font-size: 12px;
  line-height: 1.4;
  padding: 2px 8px;
  border-radius: 6px;
  background: var(--color-surface);

  &.pos {
    color: var(--color-success);
  }

  &.neg {
    color: var(--color-danger);
  }

  &.neutral {
    color: var(--color-text-secondary);
  }

  &.gold {
    color: #b8860b;
  }

  &.trait {
    color: #d4a017;
  }

  &.skill {
    color: #58a6ff;
  }

  &.equip {
    color: var(--color-text);
  }

  &.mu-tag-equip-list {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px;
  }

  &.danger {
    color: var(--color-danger);
  }

  &.stat {
    color: #58a6ff;
  }

  &.class {
    color: #bc8cff;
  }

  &.muted {
    color: var(--color-text-secondary);
  }
}

.equip-label {
  margin-right: 2px;
}

.equip-sep {
  color: var(--color-text-secondary);
}

.rarity-pill {
  font-size: 10px;
  font-weight: 600;
  padding: 0 6px;
  border: 1px solid currentColor;
  border-radius: 10px;
}
</style>
