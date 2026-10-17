<template>
  <Teleport to="body">
    <div v-if="item" class="reward-item-preview-overlay s1-mobile-surface s1-mobile-overlay" @click.self="emit('close')">
      <section class="reward-item-preview-dialog s1-mobile-dialog" role="dialog" aria-modal="true" aria-label="奖励物品预览">
        <header class="reward-item-preview-header s1-mobile-dialog-header">
          <div>
            <div class="reward-item-preview-kicker">{{ kicker }}</div>
            <h4 :style="item.rarity ? { color: rarityColor(item.rarity) } : undefined">
              {{ itemLabel(item) }}
            </h4>
          </div>
          <button type="button" class="reward-item-preview-close" aria-label="关闭" @click="emit('close')">×</button>
        </header>
        <div class="reward-item-preview-meta">
          <span v-if="item.type">{{ itemTypeLabel(item.type) }}</span>
          <span v-if="item.rarity">{{ rarityLabel(item.rarity) }}</span>
        </div>
        <p class="reward-item-preview-description">{{ item.description || item.desc || '暂无物品描述' }}</p>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { itemTypeLabel, type RewardPreviewItem } from '../itemTypes'
import { rarityColor, rarityLabel } from '../utils'

withDefaults(defineProps<{
  item: RewardPreviewItem | null
  kicker?: string
}>(), {
  kicker: '委托奖励预览',
})

const emit = defineEmits<{
  close: []
}>()

function itemLabel(item: RewardPreviewItem): string {
  const name = item.name ?? item.item_name ?? '物品'
  const quantity = item.quantity ?? 1
  return quantity > 1 ? `${name}×${quantity}` : name
}
</script>

<style>
.reward-item-preview-overlay {
  position: fixed;
  inset: 0;
  z-index: 2100;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgba(4, 8, 14, 0.68);
}
.reward-item-preview-dialog {
  width: min(420px, 100%);
  max-height: min(70vh, 560px);
  overflow: auto;
  padding: 18px;
  border: 1px solid #526174;
  border-color: var(--color-border, #526174);
  border-radius: 14px;
  background: #111821;
  background: var(--color-bg, #111821);
  color: #eef3f8;
  color: var(--color-text, #eef3f8);
  box-shadow: 0 22px 60px rgba(0, 0, 0, 0.42);
}
.reward-item-preview-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.reward-item-preview-header h4 { margin: 2px 0 0; }
.reward-item-preview-kicker,
.reward-item-preview-meta {
  color: #9daaba;
  color: var(--color-text-secondary, #9daaba);
  font-size: 0.8rem;
}
.reward-item-preview-meta { display: flex; gap: 10px; margin-top: 12px; }
.reward-item-preview-description { margin: 14px 0 0; line-height: 1.65; white-space: pre-wrap; }
.reward-item-preview-close {
  appearance: none;
  -webkit-appearance: none;
  border: 0;
  background: transparent;
  color: #eef3f8;
  color: var(--color-text, #eef3f8);
  font-size: 1.65rem;
  line-height: 1;
  cursor: pointer;
}
</style>
