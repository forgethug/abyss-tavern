<template>
  <div class="emblem-editor">
    <header class="editor-header">
      <div class="editor-live-preview">
        <SquadEmblem
          :emblem="modelValue"
          :size="76"
          title="当前完整纹章预览"
        />
        <span>当前样稿</span>
      </div>
      <div class="editor-heading">
        <span class="editor-kicker">纹章工坊</span>
        <strong>{{ combinationLabel }} 种固定素材组合</strong>
        <small>点选图块即可预览；所有素材均为酒馆内置，不接受上传。</small>
      </div>
      <div class="editor-actions" aria-label="纹章快捷操作">
        <button type="button" @click="randomize">随机灵感</button>
        <button type="button" @click="swapPrimaryAndSecondary">对调主辅</button>
        <button type="button" @click="restoreDefault">恢复默认</button>
      </div>
    </header>

    <section
      v-for="field in visualFields"
      :key="field.key"
      class="editor-section"
    >
      <header class="section-heading">
        <span>{{ field.label }}</span>
        <strong>{{ currentLabel(field.key) }}</strong>
      </header>
      <div class="visual-options">
        <label
          v-for="option in optionsFor(field.key)"
          :key="option.key"
          class="visual-option"
          :class="{ selected: modelValue[field.key] === option.key }"
        >
          <input
            class="option-input"
            type="radio"
            :name="groupName(field.key)"
            :value="option.key"
            :checked="modelValue[field.key] === option.key"
            :aria-label="`${field.label}：${option.label}`"
            @change="update(field.key, option.key)"
          />
          <SquadEmblem
            :emblem="previewFor(field.key, option.key)"
            :size="field.previewSize"
          />
          <span>{{ option.label }}</span>
        </label>
      </div>
    </section>

    <section class="editor-section color-section">
      <header class="section-heading">
        <span>配色</span>
        <strong>主色 · 辅色 · 描边</strong>
      </header>
      <div class="color-roles">
        <div
          v-for="field in colorFields"
          :key="field.key"
          class="color-role"
        >
          <header>
            <span>{{ field.label }}</span>
            <strong>{{ currentLabel(field.key) }}</strong>
          </header>
          <div class="color-options">
            <label
              v-for="option in optionsFor(field.key)"
              :key="option.key"
              class="color-option"
              :class="{ selected: modelValue[field.key] === option.key }"
              :title="option.label"
            >
              <input
                class="option-input"
                type="radio"
                :name="groupName(field.key)"
                :value="option.key"
                :checked="modelValue[field.key] === option.key"
                :aria-label="`${field.label}：${option.label}`"
                @change="update(field.key, option.key)"
              />
              <span
                class="color-swatch"
                :class="`swatch-${option.key}`"
                aria-hidden="true"
              ></span>
              <span>{{ option.label }}</span>
            </label>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, useId } from 'vue'
import SquadEmblem from './SquadEmblem.vue'
import type { SquadEmblemData, SquadFeature } from '../stores/adventurerS1'
import { normalizeSquadEmblem, safeSquadCatalogOptions } from '../squadEmblem'

const props = defineProps<{
  modelValue: SquadEmblemData
  feature: SquadFeature
}>()
const emit = defineEmits<{ 'update:modelValue': [SquadEmblemData] }>()
const editorId = useId()

const visualFields: Array<{
  key: 'frame' | 'pattern' | 'sigil'
  label: string
  previewSize: number
}> = [
  { key: 'frame', label: '轮廓', previewSize: 42 },
  { key: 'pattern', label: '底纹', previewSize: 42 },
  { key: 'sigil', label: '徽记', previewSize: 42 },
]

const colorFields: Array<{
  key: 'primary' | 'secondary' | 'accent'
  label: string
}> = [
  { key: 'primary', label: '主色' },
  { key: 'secondary', label: '辅色' },
  { key: 'accent', label: '描边' },
]
const allFields: Array<keyof SquadEmblemData> = [
  ...visualFields.map(field => field.key),
  ...colorFields.map(field => field.key),
]

function remoteOptions(key: keyof SquadEmblemData) {
  const catalog = props.feature.catalog
  if (key === 'frame') return catalog.frames
  if (key === 'pattern') return catalog.patterns
  if (key === 'sigil') return catalog.sigils
  return catalog.colors
}

function optionsFor(key: keyof SquadEmblemData) {
  return safeSquadCatalogOptions(key, remoteOptions(key))
}

function groupName(key: keyof SquadEmblemData): string {
  return `${editorId}-${key}`
}

const combinationLabel = computed(() => {
  const count = allFields.reduce(
    (total, key) => total * optionsFor(key).length,
    1,
  )
  return new Intl.NumberFormat('zh-CN').format(count)
})

function update(key: keyof SquadEmblemData, value: string) {
  if (!optionsFor(key).some(option => option.key === value)) return
  emit('update:modelValue', normalizeSquadEmblem({
    ...props.modelValue,
    [key]: value,
  }))
}

function previewFor(key: keyof SquadEmblemData, value: string): SquadEmblemData {
  return normalizeSquadEmblem({
    ...props.modelValue,
    [key]: value,
  })
}

function currentLabel(key: keyof SquadEmblemData): string {
  return optionsFor(key).find(option => option.key === props.modelValue[key])?.label || '未选择'
}

function randomize() {
  const next = { ...props.modelValue }
  for (const key of allFields) {
    const options = optionsFor(key)
    if (!options.length) continue
    next[key] = options[Math.floor(Math.random() * options.length)].key
  }
  emit('update:modelValue', normalizeSquadEmblem(next))
}

function swapPrimaryAndSecondary() {
  emit('update:modelValue', normalizeSquadEmblem({
    ...props.modelValue,
    primary: props.modelValue.secondary,
    secondary: props.modelValue.primary,
  }))
}

function restoreDefault() {
  emit(
    'update:modelValue',
    normalizeSquadEmblem(props.feature.catalog.default_emblem),
  )
}
</script>

<style scoped>
.emblem-editor {
  display: grid;
  min-width: 0;
  gap: 14px;
}

.editor-header {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px;
  border: 1px solid rgba(200, 155, 88, 0.28);
  border-radius: 12px;
  background:
    radial-gradient(circle at 8% 0%, rgba(200, 155, 88, 0.14), transparent 42%),
    var(--color-surface);
}

.editor-live-preview {
  display: flex;
  min-width: 96px;
  flex: 0 0 auto;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 10px;
  background: rgba(200, 155, 88, 0.08);
  border: 1px solid rgba(200, 155, 88, 0.24);
  border-radius: 10px;
}

.editor-live-preview span {
  color: #c89b58;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.08em;
}

.editor-heading {
  display: flex;
  min-width: 0;
  flex: 1 1 180px;
  flex-direction: column;
  gap: 3px;
}

.editor-kicker {
  color: #c89b58;
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.16em;
}

.editor-heading strong {
  color: var(--color-text);
  font-size: 15px;
}

.editor-heading small {
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.55;
}

.editor-actions {
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
  margin-left: auto;
}

.editor-actions button {
  min-height: 34px;
  padding: 6px 10px;
  color: var(--color-text);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  background: var(--color-surface);
  background: color-mix(in srgb, var(--color-surface) 84%, #c89b58 16%);
  border: 1px solid var(--color-border);
  border-color: color-mix(in srgb, var(--color-border) 74%, #c89b58 26%);
  border-radius: 8px;
}

.editor-actions button:hover,
.editor-actions button:focus-visible {
  color: #c89b58;
  border-color: #c89b58;
  outline: none;
}

.editor-section {
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background: var(--color-surface);
  background: color-mix(in srgb, var(--color-surface) 96%, #c89b58 4%);
}

.section-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
  color: var(--color-text-secondary);
  font-size: 12px;
}

.section-heading > span {
  color: var(--color-text);
  font-size: 13px;
  font-weight: 800;
}

.section-heading > strong {
  color: #c89b58;
  font-size: 12px;
}

.visual-options {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));
  gap: 7px;
}

.option-input {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}

.visual-option {
  position: relative;
  display: flex;
  min-width: 0;
  min-height: 74px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 5px;
  padding: 7px 4px 6px;
  color: var(--color-text);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
  user-select: none;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 9px;
  transition:
    border-color 0.15s ease,
    background-color 0.15s ease,
    transform 0.15s ease;
}

.visual-option:hover {
  border-color: #c89b58;
  border-color: color-mix(in srgb, var(--color-border) 36%, #c89b58 64%);
  transform: translateY(-1px);
}

.visual-option:focus-within,
.color-option:focus-within {
  border-color: #c89b58;
  outline: 2px solid rgba(200, 155, 88, 0.3);
  outline-offset: 1px;
}

.visual-option.selected {
  border-color: #c89b58;
  background:
    linear-gradient(rgba(200, 155, 88, 0.1), rgba(200, 155, 88, 0.1)),
    var(--color-surface);
  box-shadow: inset 0 0 0 1px rgba(200, 155, 88, 0.28);
}

.visual-option.selected::after {
  position: absolute;
  top: 4px;
  right: 5px;
  color: #c89b58;
  font-size: 10px;
  content: '✓';
}

.color-roles {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.color-role {
  min-width: 0;
  padding: 9px;
  border: 1px solid var(--color-border);
  border-color: color-mix(in srgb, var(--color-border) 86%, transparent);
  border-radius: 9px;
}

.color-role > header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
  color: var(--color-text-secondary);
  font-size: 11px;
}

.color-role > header span {
  color: var(--color-text);
  font-weight: 800;
}

.color-role > header strong {
  min-width: 0;
  overflow: hidden;
  color: #c89b58;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.color-options {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 5px;
}

.color-option {
  display: flex;
  min-width: 0;
  min-height: 44px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  padding: 4px 2px;
  color: var(--color-text-secondary);
  font: inherit;
  font-size: 9px;
  cursor: pointer;
  user-select: none;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 7px;
}

.color-option:hover,
.color-option.selected {
  color: var(--color-text);
  border-color: var(--color-border);
  border-color: color-mix(in srgb, var(--color-border) 54%, #c89b58 46%);
  background: var(--color-surface);
  background: color-mix(in srgb, var(--color-surface) 88%, #c89b58 12%);
}

.color-option.selected {
  border-color: #c89b58;
}

.color-swatch {
  display: block;
  width: 22px;
  height: 22px;
  border: 2px solid rgba(255, 255, 255, 0.62);
  border-radius: 50%;
  box-shadow:
    0 0 0 1px rgba(0, 0, 0, 0.3),
    inset 0 0 0 1px rgba(0, 0, 0, 0.08);
}

.swatch-crimson { background: #a83a4b; }
.swatch-azure { background: #3674a5; }
.swatch-emerald { background: #3d7f66; }
.swatch-violet { background: #76558f; }
.swatch-gold { background: #c89a3d; }
.swatch-ivory { background: #e7dfcc; }
.swatch-obsidian { background: #252a31; }
.swatch-copper { background: #a96845; }
.swatch-sapphire { background: #294a78; }
.swatch-teal { background: #2d7c79; }
.swatch-jade { background: #6f9d64; }
.swatch-rose { background: #b65a78; }
.swatch-amber { background: #cf7d32; }
.swatch-silver { background: #aeb6c2; }
.swatch-umber { background: #6a4c3b; }
.swatch-midnight { background: #2b294f; }
.swatch-coral { background: #d3685c; }
.swatch-lavender { background: #9a79b6; }

@supports not (background: color-mix(in srgb, black, white)) {
  .editor-actions button,
  .editor-section,
  .visual-option.selected,
  .color-option:hover,
  .color-option.selected {
    background: var(--color-surface);
  }
}

@media (prefers-reduced-motion: reduce) {
  .visual-option {
    transition: none;
  }

  .visual-option:hover {
    transform: none;
  }
}

@media (max-width: 720px) {
  .editor-header {
    align-items: stretch;
    flex-direction: column;
  }

  .editor-live-preview {
    align-self: center;
  }

  .editor-actions {
    width: 100%;
    justify-content: flex-start;
    margin-left: 0;
  }

  .editor-actions button {
    flex: 1 1 auto;
    min-height: 40px;
  }

  .visual-options {
    display: grid;
    grid-auto-columns: 76px;
    grid-auto-flow: column;
    grid-template-columns: none;
    justify-content: start;
    overflow-x: auto;
    overscroll-behavior-inline: contain;
    padding: 2px 1px 8px;
    scroll-snap-type: inline proximity;
  }

  .visual-option {
    scroll-snap-align: start;
  }

  .color-roles {
    grid-template-columns: 1fr;
  }

  .color-options {
    grid-template-columns: repeat(6, minmax(0, 1fr));
  }

  .color-option {
    min-height: 48px;
  }
}

@media (max-width: 440px) {
  .editor-header,
  .editor-section {
    padding: 10px;
  }

  .editor-actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
  }

  .editor-actions button:first-child {
    grid-column: 1 / -1;
  }

  .color-options {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }

  .color-option {
    font-size: 10px;
  }
}
</style>
