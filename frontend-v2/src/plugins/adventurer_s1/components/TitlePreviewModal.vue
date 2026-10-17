<template>
  <div class="title-preview-overlay s1-mobile-surface s1-mobile-overlay" @click.self="emit('close')">
    <section
      class="title-preview-dialog s1-mobile-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="title-preview-heading"
    >
      <header class="title-preview-head s1-mobile-dialog-header">
        <div>
          <small>TAVERN TITLE ARCHIVE</small>
          <h3 id="title-preview-heading">酒馆称号预览</h3>
          <p>可以提前查看称号外观、完成条件与当前进度。</p>
        </div>
        <button type="button" class="title-preview-close" aria-label="关闭称号预览" @click="emit('close')">✕</button>
      </header>

      <div class="title-preview-grid s1-mobile-dialog-body">
        <article
          v-for="preview in previews"
          :key="preview.id"
          class="title-preview-card"
          :class="`frame-${preview.frame_key}`"
        >
          <div class="title-preview-artwork">
            <TitleFrameArtwork
              v-if="isSpecialFrame(preview.frame_key)"
              :kind="preview.frame_key"
              variant="card"
            />
            <div class="title-preview-identity">
              <span
                class="title-preview-status"
                :class="{ unlocked: preview.is_available }"
              >{{ preview.is_available ? '已获得' : '尚未获得' }}</span>
              <strong>{{ preview.name }}</strong>
              <p>{{ preview.description }}</p>
            </div>
          </div>

          <div class="title-preview-condition">
            <span class="condition-label">完成条件</span>
            <p>{{ preview.condition }}</p>
            <template v-if="preview.exchange_ingredients?.length">
              <ul class="exchange-requirements">
                <li v-for="ingredient in preview.exchange_ingredients" :key="ingredient.item_name">
                  <strong :style="{ color: rarityColor(ingredient.rarity) }">
                    {{ ingredient.item_name }} ×{{ ingredient.required }}
                  </strong>
                </li>
              </ul>
              <p class="exchange-guidance">{{ preview.is_available ? '已获得，可在冒险者详情中装备。' : '前往建筑页的秘境收藏馆查看持有数量并兑换。' }}</p>
            </template>
            <template v-else>
              <div class="condition-progress-row">
                <span>{{ preview.condition_completed ? '条件已完成' : '当前进度' }}</span>
                <strong>
                  {{ preview.progress_current.toLocaleString() }}
                  /
                  {{ preview.progress_required.toLocaleString() }}
                </strong>
              </div>
              <div
                class="condition-progress"
                role="progressbar"
                :aria-valuemin="0"
                :aria-valuemax="preview.progress_required"
                :aria-valuenow="Math.min(preview.progress_current, preview.progress_required)"
              >
                <span :style="{ width: `${progressPercent(preview)}%` }"></span>
              </div>
            </template>
          </div>
        </article>
      </div>

      <footer class="title-preview-foot s1-mobile-dialog-footer">
        <span>预览不会装备称号，也不会改变当前进度。</span>
        <button type="button" @click="emit('close')">关闭</button>
      </footer>
    </section>
  </div>
</template>

<script setup lang="ts">
import type { AdventurerTitlePreview } from '../stores/adventurerS1'
import TitleFrameArtwork from './TitleFrameArtwork.vue'
import { rarityColor } from '../utils'

defineProps<{
  previews: AdventurerTitlePreview[]
}>()

const emit = defineEmits<{ close: [] }>()

type SpecialFrame = 'fate' | 'fate_companion' | 'hidden_relic'

function isSpecialFrame(frame: string): frame is SpecialFrame {
  return frame === 'fate' || frame === 'fate_companion' || frame === 'hidden_relic'
}

function progressPercent(preview: AdventurerTitlePreview): number {
  if (preview.progress_required <= 0) return 100
  return Math.min(100, Math.max(0, (preview.progress_current / preview.progress_required) * 100))
}
</script>

<style scoped lang="scss">
.title-preview-overlay {
  position: fixed;
  z-index: 1600;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgba(4, 8, 10, 0.78);
  backdrop-filter: blur(5px);
}

.title-preview-dialog {
  display: flex;
  width: min(920px, 100%);
  max-height: min(820px, 92dvh);
  flex-direction: column;
  overflow: hidden;
  border: 1px solid #38534d;
  border-radius: 16px;
  background: #12191c;
  color: #e6edf3;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.5);
}

// 由列表单独滚动；公共手机弹窗样式不能再让外壳成为第二个滚动区。
.title-preview-dialog.s1-mobile-dialog { overflow: hidden; }

.title-preview-head {
  display: flex;
  flex: 0 0 auto;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 20px;
  border-bottom: 1px solid #2d3a3e;
  background:
    radial-gradient(circle at 12% 0, rgba(84, 211, 177, 0.09), transparent 34%),
    linear-gradient(120deg, #14201f, #15191e);
}

.title-preview-head small {
  color: #54d3b1;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.18em;
}

.title-preview-head h3 {
  margin: 3px 0 4px;
  color: #f0c25c;
  font-size: 22px;
}

.title-preview-head p {
  margin: 0;
  color: #9eb0b6;
  font-size: 12px;
}

.title-preview-close {
  display: grid;
  width: 34px;
  height: 34px;
  flex: none;
  place-items: center;
  border: 1px solid #35444a;
  border-radius: 9px;
  background: #0f1518;
  color: #b7c5c9;
  cursor: pointer;
}

.title-preview-grid {
  display: grid;
  min-height: 0;
  flex: 1 1 auto;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  // 新增称号后会生成更多行，必须保留卡片的完整内容高度。
  grid-auto-rows: max-content;
  align-content: start;
  align-items: start;
  gap: 16px;
  overflow-y: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
  scrollbar-gutter: stable;
  scrollbar-width: thin;
  scrollbar-color: #38534d transparent;
  padding: 18px;
}

.title-preview-card {
  min-width: 0;
  overflow: hidden;
  border: 1px solid #39464c;
  border-radius: 14px;
  background: #11181b;
}

.title-preview-card.frame-fate {
  border-color: color-mix(in srgb, #d9b96e 65%, #8b78c9);
}

.title-preview-card.frame-fate_companion {
  border-color: color-mix(in srgb, #54d3b1 68%, #f0c25c);
}

.title-preview-artwork {
  position: relative;
  min-height: 200px;
  overflow: hidden;
  background:
    radial-gradient(circle at 82% 12%, rgba(217, 185, 110, 0.1), transparent 32%),
    radial-gradient(circle at 12% 88%, rgba(84, 211, 177, 0.08), transparent 38%),
    #141c20;
}

.title-preview-identity {
  position: relative;
  z-index: 2;
  padding: 58px 26px 22px;
}

.title-preview-identity strong {
  display: block;
  margin-top: 9px;
  color: #f1e2b4;
  font-size: 23px;
}

.title-preview-identity p {
  margin: 8px 0 0;
  color: #afbec2;
  font-size: 13px;
  line-height: 1.6;
}

.title-preview-status {
  display: inline-flex;
  padding: 3px 8px;
  border: 1px solid #56636a;
  border-radius: 999px;
  color: #a9b3b8;
  background: rgba(9, 14, 16, 0.64);
  font-size: 10px;
  font-weight: 700;
}

.title-preview-status.unlocked {
  border-color: rgba(84, 211, 177, 0.62);
  color: #75e4c5;
}

.title-preview-condition {
  padding: 16px 18px 18px;
  border-top: 1px solid #2a373c;
}

.condition-label {
  color: #d9b96e;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.12em;
}

.title-preview-condition > p {
  min-height: 42px;
  margin: 6px 0 14px;
  color: #d5dfe1;
  font-size: 13px;
  line-height: 1.55;
}

.condition-progress-row {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 7px;
  color: #8fa2a8;
  font-size: 11px;
}

.condition-progress-row strong {
  color: #f0c25c;
  font-variant-numeric: tabular-nums;
}

.condition-progress {
  height: 7px;
  overflow: hidden;
  border-radius: 999px;
  background: #263137;
}

.condition-progress span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, #2a947d, #54d3b1 62%, #f0c25c);
  transition: width 0.25s ease;
}

.title-preview-foot {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 13px 20px;
  border-top: 1px solid #2d3a3e;
  color: #86999f;
  font-size: 11px;
}

.title-preview-foot button {
  min-width: 74px;
  padding: 7px 14px;
  border: 1px solid #3d7569;
  border-radius: 8px;
  background: #173029;
  color: #78dfc3;
  cursor: pointer;
}

.title-preview-foot > span {
  flex: 1;
  min-width: 0;
  line-height: 1.5;
}

@media (max-width: 720px) {
  .title-preview-overlay {
    align-items: end;
    padding: 0;
  }

  .title-preview-dialog {
    height: 94vh;
    height: 94dvh;
    max-height: 94dvh;
    border-right: 0;
    border-bottom: 0;
    border-left: 0;
    border-radius: 18px 18px 0 0;
  }

  .title-preview-head {
    padding: 15px 16px;
  }

  .title-preview-grid {
    grid-template-columns: minmax(0, 1fr);
    gap: 12px;
    padding: 12px;
  }

  .title-preview-artwork {
    min-height: 180px;
  }

  .title-preview-identity {
    padding: 48px 22px 18px;
  }

  .title-preview-condition > p {
    min-height: 0;
  }

  .title-preview-foot {
    flex: 0 0 auto;
    padding: 11px 14px max(11px, env(safe-area-inset-bottom));
  }
}

.title-preview-card.frame-hidden_relic { border-color: #a98959; }
.frame-hidden_relic .title-preview-artwork {
  background: radial-gradient(ellipse at 78% 0, rgba(185, 154, 232, .10), transparent 44%), var(--color-surface, #141c20);
}
.frame-hidden_relic .title-preview-identity strong { color: #e0bb7d; }
:global([data-theme="light"] .frame-hidden_relic .title-preview-identity strong) { color: #825820; }
:global([data-theme="light"] .frame-hidden_relic .title-preview-identity p) { color: #665975; }

.exchange-requirements { display: grid; gap: 9px; margin: 12px 0; padding: 0; list-style: none; }
.exchange-requirements li { display: grid; gap: 3px; padding-bottom: 8px; border-bottom: 1px solid rgba(169, 137, 89, .22); }
.exchange-requirements strong { font-size: 12px; }
.title-preview-condition .exchange-guidance { min-height: 0; font-size: 11px; color: #bda983; }
:global([data-theme="light"] .frame-hidden_relic) { background: #faf6ef; }
:global([data-theme="light"] .frame-hidden_relic .title-preview-condition) { color: #44364b; }
:global([data-theme="light"] .frame-hidden_relic .title-preview-condition > p) { color: #695c72; }
</style>
