<template>
  <div
    ref="shellRef"
    class="adv-card-shell"
    :class="[titleFrameClass, { 'is-viewport-inactive': !isViewportActive }]"
  >
    <span v-if="titleFrameClass" class="title-frame-ornaments" aria-hidden="true">
      <TitleFrameArtwork :kind="titleFrameKind!" variant="card" />
    </span>
    <div
      class="adv-card"
      :class="[{ dead: adv.status === 'dead', mine: isMine }, titleFrameClass]"
      :style="{ '--adv-accent': statusColor(adv.status, inTavern) }"
      @click="emit('open', adv)"
    >
      <span
        v-if="titleFrameKind"
        class="title-card-atmosphere"
        :class="`is-${titleFrameKind}`"
        aria-hidden="true"
      ></span>
      <div class="adv-card-head">
        <span class="adv-name-wrap">
          <SquadEmblem
            v-if="adv.squad_badge"
            :emblem="adv.squad_badge.emblem ?? adv.squad_badge"
            :size="22"
            :title="adv.squad_badge.squad_name || adv.squad_badge.name || '纹章小队徽记'"
          />
          <span class="adv-name">{{ adv.name }}</span>
          <span v-if="adv.active_title" class="adv-title-badge" :title="adv.active_title.description">{{ adv.active_title.name }}</span>
        </span>
        <span
          class="adv-status"
        >{{ statusLabel(adv.status, adv.building_instance_status, adv.building_handler_key, inTavern) }}</span>
      </div>

      <div class="adv-hp">
        <div class="adv-hp-fill" :style="{ width: liveHpPercent + '%' }"></div>
        <span class="adv-hp-text">❤️ {{ liveHp }}/{{ adv.max_hp }}</span>
      </div>

      <div v-if="adv.stamina != null" class="adv-stamina">
        <div class="adv-stamina-fill" :style="{ width: staminaPercent + '%' }"></div>
        <span class="adv-stamina-text">⚡ {{ liveStamina }}/{{ adv.max_stamina ?? STAMINA_MAX }}</span>
      </div>

      <div class="adv-row">
        <span class="lv">Lv.{{ adv.level }}</span>
        <span>⚔️ {{ adv.strength }}</span>
        <span>🏃 {{ adv.agility }}</span>
        <span>🧠 {{ adv.intelligence }}</span>
        <span>🍀 {{ adv.luck }}</span>
      </div>

      <div class="adv-row secondary">
        <span>💰 {{ formatMoney(adv.gold) }}</span>
        <span>🎭 {{ adv.character_class || '无职业' }}</span>
        <span v-if="personalityText(adv.personality)">💭 {{ personalityText(adv.personality) }}</span>
      </div>

      <div class="adv-tags">
        <span v-if="adv.expedition_buff?.label" class="tag-badge buff">{{ adv.expedition_buff.label }}</span>
        <template v-if="equippedTraits.length">
          <span v-for="t in traitPreview" :key="t.name" class="tag-badge">{{ t.name }}</span>
          <span
            v-if="hiddenTraitCount > 0"
            class="tag-more"
            :title="hiddenTraitNames"
          >+{{ hiddenTraitCount }}</span>
        </template>
        <span v-else class="tag-badge empty">无特质</span>
      </div>

      <div v-if="equipPreview.length" class="adv-tags">
        <span
          v-for="eq in equipPreview"
          :key="'eq-' + eq.id"
          class="equip-badge"
          :style="{ color: rarityColor(eq.rarity) }"
        >{{ hiddenBossClueItemDisplayName(eq) }}<template v-if="eq.clue_label"> {{ eq.clue_label }}</template><template v-if="(eq.quantity ?? 1) > 1">×{{ eq.quantity }}</template></span>
      </div>

      <div
        class="adv-foot"
        :class="{ 'has-initial-rename': isMine && adv.initial_rename_available }"
        @click.stop
      >
        <div class="adv-metrics">
          <span v-if="isMine" class="badge-mine">我的</span>
          <span class="badge-score">🏅 {{ adv.latest_score == null ? '未评分' : adv.latest_score.toFixed(1) }}</span>
          <span class="badge-praise">👍 {{ adv.praise_count ?? 0 }}赞</span>
        </div>
        <div class="adv-actions">
          <button
            type="button"
            class="history-btn"
            @click="emit('view-logs', adv)"
          >📜 探险</button>
          <button
            v-if="isMine && adv.initial_rename_available"
            type="button"
            class="initial-rename-btn"
            :disabled="renaming"
            @click.stop="renameInitialName"
          >{{ renaming ? '命名中…' : '✍️ 命名一次' }}</button>
          <button
            v-if="!isMine"
            type="button"
            class="follow-btn"
            :class="{ active: adv.is_followed }"
            @click.stop="emit('toggle-follow', adv)"
          >{{ adv.is_followed ? '⭐ 已关注' : '☆ 关注' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useAdventurerS1Store, type AdventurerData } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { formatMoney } from '@/utils/format'
import { statusLabel, statusColor, rarityColor, personalityText, liveHpOf, hpPercent, liveStaminaOf, hiddenBossClueItemDisplayName } from '../utils'
import { useLiveTick } from '../composables/useLiveTick'
import { useViewportActivity } from '../composables/useViewportActivity'
import TitleFrameArtwork from './TitleFrameArtwork.vue'
import SquadEmblem from './SquadEmblem.vue'

const props = defineProps<{ adv: AdventurerData; currentUserId?: number | null }>()
const emit = defineEmits<{ open: [AdventurerData]; 'toggle-follow': [AdventurerData]; 'view-logs': [AdventurerData] }>()

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const STAMINA_MAX = 100
const { now } = useLiveTick()
const renaming = ref(false)
const shellRef = ref<HTMLElement | null>(null)
const { isViewportActive } = useViewportActivity(shellRef)
const inactiveNow = ref(now.value)

watch(isViewportActive, (active) => {
  if (!active) inactiveNow.value = now.value
})

const cardNow = computed(() => (isViewportActive.value ? now.value : inactiveNow.value))

const isMine = computed(() => props.currentUserId != null && props.adv.owner_id === props.currentUserId)
const titleFrameKind = computed<'fate' | 'fate_companion' | 'hidden_relic' | null>(() => {
  const frame = props.adv.active_title?.frame_key
  return frame === 'fate' || frame === 'fate_companion' || frame === 'hidden_relic' ? frame : null
})
const titleFrameClass = computed(() => {
  return titleFrameKind.value ? `title-frame--${titleFrameKind.value}` : ''
})
/** 入座不改变 status，名片需结合大堂座位判断是否在酒馆 */
const inTavern = computed(() => {
  if (props.adv.tavern_seated) return true
  return store.tavernSeats.some((s) => s.adventurer?.id === props.adv.id)
})
const equipPreview = computed(() => props.adv.equipment ?? [])
const TRAIT_PREVIEW_LIMIT = 4
const equippedTraits = computed(() => (props.adv.traits ?? []).filter(t => t.equipped !== false))
const traitPreview = computed(() => equippedTraits.value.slice(0, TRAIT_PREVIEW_LIMIT))
const hiddenTraitCount = computed(() => Math.max(0, equippedTraits.value.length - TRAIT_PREVIEW_LIMIT))
const hiddenTraitNames = computed(() => equippedTraits.value.slice(TRAIT_PREVIEW_LIMIT).map(t => t.name).join('、'))

const liveHp = computed(() => liveHpOf(props.adv, cardNow.value))
const liveHpPercent = computed(() => hpPercent(props.adv, cardNow.value))

const liveStamina = computed(() => liveStaminaOf(props.adv, cardNow.value))
const staminaPercent = computed(() => Math.max(0, Math.min(100, (liveStamina.value / (props.adv.max_stamina ?? STAMINA_MAX)) * 100)))

async function renameInitialName() {
  if (!isMine.value || !props.adv.initial_rename_available || renaming.value) return
  const nextName = window.prompt(
    '这是唯一一次初始命名机会。只有命名成功才会消耗；请输入中文或英文字母组成的新名字。',
    '',
  )
  if (nextName === null) return
  const normalized = nextName.trim()
  const maxLength = store.options?.name_max_length ?? 7
  if (!normalized) {
    message.error('请输入冒险者姓名')
    return
  }
  if (normalized.length > maxLength) {
    message.error(`姓名过长（最多 ${maxLength} 个字）`)
    return
  }
  if (!/^[一-鿿a-zA-Z]+$/.test(normalized)) {
    message.error('姓名只能包含中文或英文字母，不能有数字、空格或符号')
    return
  }

  renaming.value = true
  try {
    const res = await store.renameInitialAdventurer(props.adv.id, normalized)
    if (res.code === 200) {
      message.success(`命名成功：${normalized}`)
    } else {
      message.error(res.message || '命名失败，本次机会仍为你保留')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '命名失败，本次机会仍为你保留')
  } finally {
    renaming.value = false
  }
}
</script>

<style scoped lang="scss">
.adv-card-shell {
  position: relative;
  z-index: 0;
  min-width: 0;
  height: 100%;
}

.adv-card-shell.is-viewport-inactive,
.adv-card-shell.is-viewport-inactive *,
.adv-card-shell.is-viewport-inactive::before,
.adv-card-shell.is-viewport-inactive::after,
.adv-card-shell.is-viewport-inactive *::before,
.adv-card-shell.is-viewport-inactive *::after {
  animation-play-state: paused !important;
}

.adv-card-shell:hover {
  z-index: 1;
}
.adv-card-shell.title-frame--fate,
.adv-card-shell.title-frame--fate_companion,
.adv-card-shell.title-frame--fate:hover,
.adv-card-shell.title-frame--fate_companion:hover{
  /* 称号外框默认与 hover 使用同一最高层级，伸出线条不会在移入鼠标后才浮到最上方。 */
  z-index: 4;
}

.adv-card-shell > .adv-card {
  height: 100%;
  box-sizing: border-box;
}

.adv-card-shell.title-frame--fate > .adv-card:hover,
.adv-card-shell.title-frame--fate_companion > .adv-card:hover{
  /* 命途外壳与内卡必须保持同一基准线，悬停时禁止只移动内卡。 */
  transform: none;
}

.adv-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 12px;
  cursor: pointer;
  transition: border-color 0.15s, transform 0.1s, box-shadow 0.15s;

  &:hover {
    border-color: var(--color-primary);
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
  }

  &.mine {
    border-color: var(--color-primary);
    box-shadow: 0 0 0 1px var(--color-primary) inset;
  }

  &.dead {
    opacity: 0.6;
    filter: grayscale(0.4);
  }
}

.adv-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}

.adv-name {
  font-size: 15px;
  font-weight: 700;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.adv-name-wrap {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 6px;
}

.adv-title-badge {
  flex: none;
  padding: 1px 6px;
  border: 1px solid color-mix(in srgb, var(--title-frame-accent, var(--card-brass)) 45%, transparent);
  border-radius: 999px;
  color: var(--title-frame-accent, var(--card-brass));
  background: color-mix(in srgb, var(--title-frame-accent, var(--card-brass)) 10%, transparent);
  font-size: 10px;
  font-weight: 700;
}

.adv-card.title-frame--fate {
  --title-frame-accent: #d9b96e;
  padding-top: 47px;
  border-color: color-mix(in srgb, #d9b96e 68%, #8b78c9);
  box-shadow: inset 0 0 0 1px rgba(139, 120, 201, 0.18), 0 6px 18px rgba(139, 120, 201, 0.12);
}

.adv-card.title-frame--fate_companion {
  --title-frame-accent: #54d3b1;
  padding-top: 47px;
  border-color: color-mix(in srgb, #54d3b1 68%, #f0c25c);
  box-shadow: inset 0 0 0 1px rgba(240, 194, 92, 0.18), 0 6px 18px rgba(40, 168, 139, 0.14);
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
  background:
    radial-gradient(circle at 12% 14%, rgba(84, 211, 177, 0.12), transparent 31%),
    radial-gradient(circle at 88% 82%, rgba(240, 194, 92, 0.1), transparent 36%),
    repeating-linear-gradient(124deg, rgba(84, 211, 177, 0.025) 0 1px, transparent 1px 25px),
    linear-gradient(145deg, rgba(42, 148, 125, 0.04), transparent 62%),
    var(--color-surface, #161b22);
}

:global([data-theme="light"] .adv-card.title-frame--fate) {
  border-color: #9a6905;
  background:
    radial-gradient(circle at 72% 0, rgba(93, 58, 154, .09), transparent 34%),
    radial-gradient(circle at 12% 92%, rgba(167, 116, 8, .075), transparent 38%),
    linear-gradient(145deg, #fbf8f0, #ffffff 64%);
  box-shadow: inset 0 0 0 1px rgba(93, 58, 154, .14), 0 7px 18px rgba(112, 75, 0, .12);
}

:global([data-theme="light"] .adv-card.title-frame--fate_companion) {
  border-color: #167e69;
  background:
    radial-gradient(circle at 12% 14%, rgba(22, 126, 105, .09), transparent 32%),
    radial-gradient(circle at 88% 82%, rgba(165, 112, 0, .075), transparent 38%),
    linear-gradient(145deg, #f1faf7, #ffffff 64%);
  box-shadow: inset 0 0 0 1px rgba(165, 112, 0, .13), 0 7px 18px rgba(13, 101, 83, .13);
}

.adv-card.title-frame--fate::before {
  height: 4px;
  background: linear-gradient(90deg, #8b78c9, #d9b96e, #8b78c9);
}


.adv-status {
  flex: none;
  font-size: 11px;
  font-weight: 600;
  padding: 1px 8px;
  border-radius: 10px;
}

.adv-hp {
  position: relative;
  height: 16px;
  background: var(--color-bg);
  border-radius: 8px;
  overflow: hidden;
  margin-bottom: 8px;
}

.adv-hp-fill {
  position: absolute;
  inset: 0 auto 0 0;
  background: linear-gradient(90deg, #dc3545, #fd7e14);
  transition: width 0.3s;
}

.adv-hp-text {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  color: #fff;
  text-shadow: 0 0 2px rgba(0, 0, 0, 0.6);
}

.adv-stamina {
  position: relative;
  height: 12px;
  background: var(--color-bg);
  border-radius: 6px;
  overflow: hidden;
  margin-bottom: 8px;
}

.adv-stamina-fill {
  position: absolute;
  inset: 0 auto 0 0;
  background: linear-gradient(90deg, #0d6efd, #20c997);
  transition: width 0.3s;
}

.adv-stamina-text {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 600;
  color: #fff;
  text-shadow: 0 0 2px rgba(0, 0, 0, 0.6);
}

.adv-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
  color: var(--color-text);
  margin-bottom: 6px;

  .lv {
    font-weight: 700;
    color: var(--color-primary);
  }

  &.secondary {
    color: var(--color-text-secondary);
  }
}

.adv-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 6px;
}

.tag-badge,
.equip-badge {
  font-size: 11px;
  padding: 1px 6px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);

  &.empty {
    color: var(--color-text-secondary);
    opacity: 0.6;
  }

  &.buff {
    border-color: #d97706;
    color: #b45309;
    background: rgba(217, 119, 6, 0.12);
  }
}

.tag-more {
  display: inline-flex;
  align-items: center;
  min-width: 24px;
  padding: 1px 6px;
  border: 1px dashed color-mix(in srgb, var(--card-brass) 42%, var(--color-border));
  border-radius: 8px;
  background: color-mix(in srgb, var(--card-brass) 6%, var(--color-bg));
  color: var(--card-brass);
  font-size: 11px;
  font-weight: 700;
  cursor: help;
}

.adv-foot {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.badge-mine,
.badge-score {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
}

.badge-praise {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 8px;
  background: rgba(255, 193, 7, 0.14);
  color: #b45309;
  font-weight: 700;
}

.badge-mine {
  color: #fff;
  background: var(--color-primary);
}

.badge-score {
  color: #f0883e;
  background: rgba(240, 136, 62, 0.14);
}

.history-btn {
  font-size: 12px;
  min-height: 36px;
  padding: 6px 14px;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all 0.15s;

  &:hover {
    border-color: var(--color-primary);
    color: var(--color-text);
  }
}

.initial-rename-btn {
  font-size: 12px;
  min-height: 36px;
  padding: 6px 14px;
  color: var(--color-warning);
  background: color-mix(in srgb, var(--color-warning) 10%, var(--color-bg));
  border: 1px solid color-mix(in srgb, var(--color-warning) 55%, var(--color-border));
  border-radius: 12px;
  cursor: pointer;

  &:hover:not(:disabled) {
    background: color-mix(in srgb, var(--color-warning) 18%, var(--color-bg));
  }

  &:disabled {
    opacity: 0.65;
    cursor: wait;
  }
}

.follow-btn {
  margin-left: auto;
  font-size: 11px;
  padding: 2px 10px;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all 0.15s;

  &:hover {
    border-color: var(--color-primary);
    color: var(--color-text);
  }

  &.active {
    color: #f0883e;
    border-color: #f0883e;
  }
}

/* 冒险者名牌：驻地档案卡，而不是通用信息卡。 */
.adv-card {
  --card-brass: var(--adv-accent, var(--color-primary));
  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  border-color: color-mix(in srgb, var(--card-brass) 38%, var(--color-border));
  border-radius: 14px;
  padding: 14px;
  background:
    radial-gradient(circle at 100% 0, color-mix(in srgb, var(--card-brass) 15%, transparent), transparent 46%),
    linear-gradient(145deg, color-mix(in srgb, var(--card-brass) 7%, var(--color-surface)), var(--color-surface) 58%);
  box-shadow:
    inset 0 1px color-mix(in srgb, var(--card-brass) 18%, transparent),
    0 5px 16px color-mix(in srgb, var(--card-brass) 8%, transparent);
  transition: border-color 0.2s, transform 0.2s, box-shadow 0.2s;

  &::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 3px;
    background: linear-gradient(90deg, var(--card-brass), color-mix(in srgb, var(--card-brass) 35%, transparent));
    box-shadow: 0 0 14px color-mix(in srgb, var(--card-brass) 42%, transparent);
    transition: width 0.25s ease;
  }

  &::after {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(105deg, transparent 25%, color-mix(in srgb, var(--card-brass) 12%, transparent) 48%, transparent 68%);
    transform: translateX(-120%);
    pointer-events: none;
  }

  &:hover {
    border-color: color-mix(in srgb, var(--card-brass) 60%, var(--color-border));
    transform: translateY(-3px);
    box-shadow: 0 10px 26px rgba(0, 0, 0, 0.14);

    &::before { filter: saturate(1.25) brightness(1.08); }
    &::after { animation: adv-card-sweep 0.75s ease-out; }
  }

  &.mine {
    border-color: color-mix(in srgb, var(--card-brass) 70%, var(--color-border));
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--card-brass) 30%, transparent) inset;

    &::before { animation: adv-card-beacon 2.8s ease-in-out infinite; }
  }
}

.adv-name { letter-spacing: 0.02em; }
.adv-status {
  color: var(--card-brass);
  border: 1px solid color-mix(in srgb, var(--card-brass) 40%, transparent);
  background: color-mix(in srgb, var(--card-brass) 13%, var(--color-surface));
  box-shadow: inset 0 0 8px color-mix(in srgb, var(--card-brass) 7%, transparent);
}
.adv-foot {
  margin-top: auto;
  padding-top: 9px;
  border-top: 1px dashed color-mix(in srgb, var(--card-brass) 14%, var(--color-border));
  display: flex;
  flex-direction: column;
  align-items: stretch;
  min-height: 32px;
  gap: 5px;
  overflow: visible;
}
.adv-metrics { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; font-size: 11px; }
.adv-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
.adv-actions > button:only-child { grid-column: 1 / -1; }
.adv-actions > button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 0;
  min-height: 28px;
  margin-left: 0;
  padding: 3px 4px;
  border: 1px solid color-mix(in srgb, var(--card-brass) 26%, var(--color-border));
  border-radius: 8px;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.35;
  white-space: normal;
  overflow-wrap: anywhere;
}
.adv-hp,
.adv-stamina {
  border: 0;
  border-radius: 999px;
  background: color-mix(in srgb, var(--color-text) 9%, var(--color-bg));
  box-shadow: none;
}
.adv-hp::after,
.adv-stamina::after {
  content: none;
}
.adv-hp-fill {
  border-radius: 999px;
  background: linear-gradient(90deg, #b83950, #df665d);
  box-shadow: none;
}
.adv-stamina-fill {
  border-radius: 999px;
  background: linear-gradient(90deg, #477fae, #59a6a5);
  box-shadow: none;
}
.adv-hp-text,
.adv-stamina-text {
  z-index: 2;
  color: #fff;
  text-shadow:
    0 1px 2px rgba(0, 0, 0, 0.9),
    0 0 1px rgba(0, 0, 0, 0.9);
  letter-spacing: 0.01em;
}
.adv-row .lv { color: var(--card-brass); }
.adv-row:not(.secondary) > span {
  padding: 2px 5px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--card-brass) 8%, transparent);
}
.adv-row.secondary {
  padding: 6px 8px;
  border-left: 2px solid color-mix(in srgb, var(--card-brass) 55%, transparent);
  border-radius: 0 7px 7px 0;
  background: color-mix(in srgb, var(--card-brass) 5%, transparent);
}
.tag-badge,
.equip-badge {
  border-color: color-mix(in srgb, var(--card-brass) 22%, var(--color-border));
  border-radius: 6px;
  background: color-mix(in srgb, var(--card-brass) 8%, var(--color-bg));
}
.badge-mine { background: var(--card-brass); }
.history-btn {
  min-height: 0;
  padding: 2px 7px;
  border-color: color-mix(in srgb, var(--card-brass) 34%, var(--color-border));
  border-radius: 8px;
  background: color-mix(in srgb, var(--card-brass) 8%, var(--color-bg));
  color: color-mix(in srgb, var(--card-brass) 68%, var(--color-text));
  font-size: 11px;
  font-weight: 600;
  line-height: 1.35;
  letter-spacing: 0;
  box-shadow: none;
}
.history-btn:hover {
  border-color: var(--card-brass);
  background: color-mix(in srgb, var(--card-brass) 14%, var(--color-bg));
  color: var(--color-text);
  box-shadow: none;
  transform: none;
}
.initial-rename-btn {
  min-height: 0;
  padding: 2px 7px;
  border-radius: 8px;
  font-size: 10px;
  line-height: 1.35;
}

/* 白天主题提高纸张感和雾蓝对比，避免整张名片发灰。 */
:global([data-theme="light"] .adv-card) {
  border-color: color-mix(in srgb, var(--card-brass) 45%, #c9d8e5);
  background:
    radial-gradient(circle at 100% 0, color-mix(in srgb, var(--card-brass) 14%, transparent), transparent 48%),
    linear-gradient(145deg, color-mix(in srgb, var(--card-brass) 6%, #fff), #fff 62%);
  color: #1f2d3a;
  box-shadow: inset 0 1px #fff, 0 7px 18px rgba(47, 91, 130, 0.1);
}
:global([data-theme="light"] .adv-card:hover) {
  border-color: color-mix(in srgb, var(--card-brass) 62%, #aebdcc);
  box-shadow: 0 11px 26px rgba(48, 83, 116, 0.14);
}
:global([data-theme="light"] .adv-card.mine) {
  background:
    radial-gradient(circle at 100% 0, color-mix(in srgb, var(--card-brass) 18%, transparent), transparent 48%),
    linear-gradient(145deg, color-mix(in srgb, var(--card-brass) 8%, #fff), #fff 62%);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--card-brass) 24%, transparent), 0 6px 18px rgba(48, 83, 116, 0.1);
}
:global([data-theme="light"] .tag-badge),
:global([data-theme="light"] .equip-badge),
:global([data-theme="light"] .history-btn),
:global([data-theme="light"] .initial-rename-btn),
:global([data-theme="light"] .follow-btn) {
  background: color-mix(in srgb, var(--card-brass) 7%, #fff);
  border-color: color-mix(in srgb, var(--card-brass) 24%, #d7e3ec);
}
:global([data-theme="light"] .adv-name),
:global([data-theme="light"] .adv-row) { color: #1f2d3a; }
:global([data-theme="light"] .adv-row.secondary),
:global([data-theme="light"] .adv-seat-chatter) { color: #526779; }
:global([data-theme="light"] .adv-foot) {
  border-top-color: color-mix(in srgb, var(--card-brass) 22%, #dce7ef);
}
:global([data-theme="light"] .adv-hp),
:global([data-theme="light"] .adv-stamina) {
  background: #e7edf2;
}

@keyframes adv-card-sweep {
  to { transform: translateX(120%); }
}

@keyframes adv-card-beacon {
  50% {
    opacity: 0.55;
    box-shadow: 0 0 16px color-mix(in srgb, var(--card-brass) 68%, transparent);
  }
}

@media (max-width: 640px) {
  .adv-card {
    padding: 13px;
    border-radius: 12px;

    &:hover { transform: none; box-shadow: inset 0 1px color-mix(in srgb, var(--color-text) 4%, transparent); }
    &:active { transform: scale(0.985); border-color: var(--card-brass); }
    &:hover::after { animation: none; }
  }

  .adv-card-head { align-items: flex-start; }
  .adv-name { white-space: normal; overflow-wrap: anywhere; line-height: 1.25; }
  .adv-row { gap: 6px 10px; }
  .adv-hp { height: 19px; }
  .adv-stamina { height: 15px; }
  .adv-foot {
    align-items: stretch;
    gap: 5px;
    min-height: 0;
    overflow: visible;
  }
  .badge-mine,
  .badge-score,
  .badge-praise {
    padding: 3px 6px;
    border: 1px solid transparent;
  }
  .badge-score { border-color: rgba(240, 136, 62, 0.24); }
  .badge-praise { border-color: rgba(255, 193, 7, 0.24); }
  .history-btn { min-height: 28px; padding: 3px 6px; }
  .initial-rename-btn {
    flex: 1 1 100%;
    min-height: 28px;
    padding: 3px 6px;
  }
  .follow-btn { min-height: 28px; margin-left: 0; padding: 3px 7px; }
}

.adv-card.title-frame--fate {
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
  background:
    repeating-linear-gradient(135deg, rgba(217, 185, 110, 0.025) 0 1px, transparent 1px 22px),
    radial-gradient(circle at 82% 12%, rgba(217, 185, 110, 0.1), transparent 32%),
    radial-gradient(circle at 12% 88%, rgba(139, 120, 201, 0.09), transparent 38%),
    linear-gradient(145deg, rgba(139, 120, 201, 0.03), transparent 62%),
    var(--color-surface, #161b22);
}

.title-frame-ornaments {
  position: absolute;
  z-index: 1;
  inset: 0;
  overflow: visible;
  border-radius: inherit;
  pointer-events: none;
}

.adv-card > :not(.title-frame-ornaments) {
  position: relative;
  z-index: 2;
}

.adv-card > .title-card-atmosphere {
  position: absolute;
  z-index: 0;
  inset: 0;
  overflow: hidden;
  border-radius: inherit;
  pointer-events: none;
}

.title-card-atmosphere.is-fate_companion {
  background:
    radial-gradient(circle at 18% 12%, rgba(84, 211, 177, 0.13), transparent 34%),
    radial-gradient(circle at 84% 82%, rgba(240, 194, 92, 0.11), transparent 40%),
    linear-gradient(145deg, rgba(84, 211, 177, 0.025), transparent 46%, rgba(240, 194, 92, 0.025));
}

.title-card-atmosphere::before,
.title-card-atmosphere::after {
  content: '';
  position: absolute;
  pointer-events: none;
}

.title-card-atmosphere.is-fate {
  background:
    radial-gradient(ellipse at 82% 12%, rgba(236, 207, 132, 0.14), transparent 36%),
    radial-gradient(ellipse at 14% 88%, rgba(139, 120, 201, 0.13), transparent 42%),
    linear-gradient(145deg, rgba(217, 185, 110, 0.022), transparent 48%, rgba(139, 120, 201, 0.03));
}

.title-card-atmosphere.is-fate::before {
  top: -42%;
  right: -54%;
  width: 118%;
  aspect-ratio: 1;
  border-radius: 50%;
  background:
    conic-gradient(
      from 28deg,
      transparent 0 18%,
      rgba(217, 185, 110, 0.08) 22%,
      transparent 30% 54%,
      rgba(139, 120, 201, 0.11) 60%,
      transparent 68% 100%
    );
  animation: fate-atmosphere-orbit 18s linear infinite;
}

.title-card-atmosphere.is-fate::after {
  inset: -30%;
  background-image:
    radial-gradient(circle, rgba(255, 240, 181, 0.92) 0 1px, transparent 1.8px),
    radial-gradient(circle, rgba(166, 145, 224, 0.78) 0 1.15px, transparent 2px),
    radial-gradient(circle, rgba(217, 185, 110, 0.52) 0 0.9px, transparent 1.7px);
  background-position: 16px 26px, 68px 38px, 102px 92px;
  background-size: 88px 88px, 137px 137px, 191px 191px;
  opacity: 0.55;
  transform: translate3d(5%, -5%, 0);
  animation: fate-starfield-drift 19s linear infinite;
}

[data-theme="light"] .title-card-atmosphere.is-fate {
  background:
    radial-gradient(ellipse at 82% 12%, rgba(151, 101, 0, 0.085), transparent 36%),
    radial-gradient(ellipse at 14% 88%, rgba(93, 58, 154, 0.075), transparent 42%),
    linear-gradient(145deg, rgba(151, 101, 0, 0.016), transparent 48%, rgba(93, 58, 154, 0.02));
}

[data-theme="light"] .title-card-atmosphere.is-fate::after {
  background-image:
    radial-gradient(circle, rgba(126, 81, 0, 0.54) 0 1px, transparent 1.8px),
    radial-gradient(circle, rgba(93, 58, 154, 0.46) 0 1.15px, transparent 2px),
    radial-gradient(circle, rgba(151, 101, 0, 0.32) 0 0.9px, transparent 1.7px);
  opacity: 0.4;
}


.title-frame-rail,
.title-frame-edge-mark,
.title-frame-fate-weave {
  position: absolute;
  display: block;
}

.title-frame-fate-weave {
  inset: 0;
  width: 100%;
  height: 100%;
}

.fate-frame-line {
  fill: none;
  vector-effect: non-scaling-stroke;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.fate-frame-line-outer {
  stroke: #d9b96e;
  stroke-width: 3.5;
  filter: drop-shadow(0 0 4px rgba(217, 185, 110, 0.58));
}

.fate-frame-line-inner {
  stroke: #8b78c9;
  stroke-width: 1.8;
  filter: drop-shadow(0 0 3px rgba(139, 120, 201, 0.66));
}

.rail-top,
.rail-bottom {
  left: 0;
  width: 100%;
  height: 14px;
  -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='40' height='14' viewBox='0 0 40 14'%3E%3Cpath d='M0 7 Q10 1 20 7 T40 7' fill='none' stroke='white' stroke-width='5' stroke-linecap='round'/%3E%3C/svg%3E") center / 40px 14px repeat-x;
  mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='40' height='14' viewBox='0 0 40 14'%3E%3Cpath d='M0 7 Q10 1 20 7 T40 7' fill='none' stroke='white' stroke-width='5' stroke-linecap='round'/%3E%3C/svg%3E") center / 40px 14px repeat-x;
}
.rail-top { top: 0; }
.rail-bottom { bottom: 0; transform: scaleY(-1); }
.rail-left,
.rail-right {
  top: 0;
  width: 14px;
  height: 100%;
  -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='40' viewBox='0 0 14 40'%3E%3Cpath d='M7 0 Q13 10 7 20 T7 40' fill='none' stroke='white' stroke-width='5' stroke-linecap='round'/%3E%3C/svg%3E") center / 14px 40px repeat-y;
  mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='40' viewBox='0 0 14 40'%3E%3Cpath d='M7 0 Q13 10 7 20 T7 40' fill='none' stroke='white' stroke-width='5' stroke-linecap='round'/%3E%3C/svg%3E") center / 14px 40px repeat-y;
}
.rail-left { left: 0; }
.rail-right { right: 0; transform: scaleX(-1); }

.title-frame--fate .title-frame-edge-mark {
  top: 38px;
  right: -24px;
  left: auto;
  width: 35px;
  height: 48px;
  border: 1px solid color-mix(in srgb, #d9b96e 76%, #8b78c9);
  border-right: 0;
  border-radius: 55% 0 0 55%;
  background: radial-gradient(circle at 42% 50%, #5c4d7f 0 18%, #29243d 20% 52%, #b9974f 54% 58%, #201c30 61%);
  box-shadow: -3px 0 12px rgba(139, 120, 201, 0.42), inset 2px 0 6px rgba(255, 240, 181, 0.2);
}

.title-frame--fate .title-frame-edge-mark::before {
  content: '';
  position: absolute;
  top: 10px;
  left: 7px;
  width: 20px;
  height: 26px;
  border: 1px solid rgba(255, 240, 181, 0.72);
  border-radius: 50%;
  background: conic-gradient(from 22.5deg, transparent 0 12%, rgba(217, 185, 110, 0.75) 13% 15%, transparent 16% 37%, rgba(139, 120, 201, 0.9) 38% 41%, transparent 42% 100%);
}

.title-frame--fate .title-frame-edge-mark::after {
  content: '';
  position: absolute;
  top: 19px;
  left: 13px;
  width: 8px;
  height: 8px;
  border: 1px solid #fff0b5;
  background: #8b78c9;
  box-shadow: 0 0 6px rgba(255, 240, 181, 0.7);
  transform: rotate(45deg);
}

@keyframes title-frame-flow-x { to { background-position: 200% 0; } }
@keyframes title-frame-flow-y { to { background-position: 0 200%; } }
@keyframes fate-atmosphere-orbit {
  to { transform: rotate(360deg); }
}
@keyframes fate-starfield-drift {
  to {
    transform: translate3d(-5%, 5%, 0);
  }
}

@media (prefers-reduced-motion: reduce) {
  .adv-card,
  .adv-card::after,
  .adv-card::before,
  .title-card-atmosphere::before,
  .title-card-atmosphere::after,
  .title-frame-rail,
  .adv-hp-fill,
  .adv-stamina-fill { animation: none; transition: none; }
}

.adv-card-shell.title-frame--hidden_relic,
.adv-card-shell.title-frame--hidden_relic:hover { z-index: 4; }
.adv-card-shell.title-frame--hidden_relic > .adv-card:hover { transform: none; }
.adv-card.title-frame--hidden_relic {
  --title-frame-accent: #e0bb7d;
  padding-top: 52px;
  border-color: #9d7d51;
  border-radius: 12px;
  background:
    radial-gradient(ellipse at 78% 0, rgba(185, 154, 232, .10), transparent 42%),
    linear-gradient(150deg, rgba(209, 175, 113, .045), transparent 50%),
    var(--color-surface, #161b22);
  box-shadow: inset 0 0 0 1px rgba(185, 154, 232, .10), 0 7px 20px rgba(76, 45, 94, .12);
}
.adv-card.title-frame--hidden_relic .adv-name-wrap { flex-wrap: wrap; }
.adv-card.title-frame--hidden_relic .adv-title-badge { border-radius: 4px; letter-spacing: .06em; }
.adv-card.title-frame--hidden_relic::before,
.adv-card.title-frame--hidden_relic.mine::before { background: none; box-shadow: none; animation: none; }
:global([data-theme="light"] .adv-card.title-frame--hidden_relic) {
  --title-frame-accent: #825820;
  border-color: #a3824d;
  background: linear-gradient(145deg, rgba(198, 122, 35, .07), transparent 60%), var(--color-surface, #fff);
}

</style>
