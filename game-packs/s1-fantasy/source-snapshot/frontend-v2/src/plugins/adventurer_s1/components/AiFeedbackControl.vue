<template>
  <div class="ai-feedback-control" @click.stop>
    <button
      type="button"
      class="ai-feedback-trigger"
      :class="{ rated: hasRating }"
      :aria-expanded="open"
      :title="`${target.content_label} AI 反馈`"
      @click="open = !open"
    >
      <span aria-hidden="true">{{ hasRating ? '✓' : '✨' }}</span>
      <span>{{ hasRating ? '已评 AI' : '评 AI' }}</span>
    </button>

    <Transition name="feedback-pop">
      <section v-if="open" class="ai-feedback-popover" aria-label="AI 反馈评分">
        <header>
          <strong>{{ target.content_label }}</strong>
          <button type="button" aria-label="关闭评分" @click="open = false">×</button>
        </header>
        <p>只看这次成品，两项都选 1–5 分。</p>

        <div class="score-row">
          <span>AI 质量</span>
          <div class="score-buttons" aria-label="AI 质量评分">
            <button
              v-for="score in 5"
              :key="`quality-${score}`"
              type="button"
              :class="{ active: score <= qualityScore }"
              :aria-label="`AI 质量 ${score} 分`"
              @click="qualityScore = score"
            >★</button>
          </div>
        </div>

        <div class="score-row preference">
          <span>个人喜好</span>
          <div class="score-buttons" aria-label="个人喜好评分">
            <button
              v-for="score in 5"
              :key="`preference-${score}`"
              type="button"
              :class="{ active: score <= preferenceScore }"
              :aria-label="`个人喜好 ${score} 分`"
              @click="preferenceScore = score"
            >♥</button>
          </div>
        </div>

        <footer>
          <small>1 低 · 5 高</small>
          <button
            type="button"
            class="submit-feedback"
            :disabled="submitting || !qualityScore || !preferenceScore"
            @click="submit"
          >{{ submitting ? '保存中…' : (hasRating ? '更新评分' : '提交评分') }}</button>
        </footer>
      </section>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { useAdventurerS1Store, type AiFeedbackTarget } from '../stores/adventurerS1'

const props = defineProps<{
  target: AiFeedbackTarget
}>()

const emit = defineEmits<{
  updated: [target: AiFeedbackTarget]
}>()

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const open = ref(false)
const submitting = ref(false)
const qualityScore = ref(0)
const preferenceScore = ref(0)

const hasRating = computed(() => (
  (props.target.quality_score ?? 0) >= 1
  && (props.target.preference_score ?? 0) >= 1
))

function syncScores() {
  qualityScore.value = props.target.quality_score ?? 0
  preferenceScore.value = props.target.preference_score ?? 0
}

watch(
  () => [
    props.target.target_type,
    props.target.target_id,
    props.target.quality_score,
    props.target.preference_score,
  ],
  syncScores,
  { immediate: true },
)

async function submit() {
  if (!qualityScore.value || !preferenceScore.value || submitting.value) return
  submitting.value = true
  try {
    const response = await store.submitAiFeedback(
      props.target,
      qualityScore.value,
      preferenceScore.value,
    )
    if (response.code !== 200 || !response.data) {
      message.error(response.message || '评分保存失败')
      return
    }
    const updated = response.data as AiFeedbackTarget
    qualityScore.value = updated.quality_score ?? qualityScore.value
    preferenceScore.value = updated.preference_score ?? preferenceScore.value
    emit('updated', updated)
    open.value = false
    message.success('AI 反馈已记下，谢谢')
  } catch (error: any) {
    message.error(error?.response?.data?.message || '评分保存失败，请稍后再试')
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.ai-feedback-control {
  position: relative;
  display: inline-flex;
  flex: 0 0 auto;
}

.ai-feedback-trigger {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-height: 28px;
  padding: 3px 9px;
  border: 1px solid color-mix(in srgb, var(--color-primary, #a78bfa) 42%, transparent);
  border-radius: 999px;
  background: color-mix(in srgb, var(--color-primary, #a78bfa) 9%, transparent);
  color: var(--text-secondary, #c7c3d4);
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
}

.ai-feedback-trigger:hover,
.ai-feedback-trigger.rated {
  color: var(--color-primary, #c4b5fd);
  border-color: color-mix(in srgb, var(--color-primary, #a78bfa) 65%, transparent);
}

.ai-feedback-popover {
  position: absolute;
  z-index: 80;
  right: 0;
  bottom: calc(100% + 7px);
  width: min(268px, calc(100vw - 32px));
  padding: 12px;
  border: 1px solid color-mix(in srgb, var(--color-primary, #a78bfa) 34%, #2d2940);
  border-radius: 12px;
  background: var(--surface-elevated, #201d2b);
  box-shadow: 0 12px 34px rgba(0, 0, 0, .38);
  color: var(--text-primary, #f4f1fa);
}

.ai-feedback-popover header,
.ai-feedback-popover footer,
.score-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.ai-feedback-popover header button {
  border: 0;
  background: transparent;
  color: var(--text-secondary, #aaa4b8);
  font-size: 18px;
  cursor: pointer;
}

.ai-feedback-popover p {
  margin: 4px 0 10px;
  color: var(--text-secondary, #aaa4b8);
  font-size: 11px;
}

.score-row {
  min-height: 33px;
  font-size: 12px;
}

.score-buttons {
  display: inline-flex;
  gap: 2px;
}

.score-buttons button {
  width: 27px;
  height: 27px;
  padding: 0;
  border: 0;
  background: transparent;
  color: #55505f;
  font-size: 20px;
  cursor: pointer;
  transition: color .12s ease, transform .12s ease;
}

.score-buttons button:hover {
  transform: translateY(-1px);
}

.score-buttons button.active {
  color: #f5bf42;
}

.score-row.preference .score-buttons button.active {
  color: #f07f9b;
}

.ai-feedback-popover footer {
  margin-top: 9px;
  padding-top: 9px;
  border-top: 1px solid rgba(255, 255, 255, .08);
}

.ai-feedback-popover footer small {
  color: var(--text-muted, #888291);
}

.submit-feedback {
  min-height: 29px;
  padding: 4px 10px;
  border: 0;
  border-radius: 7px;
  background: var(--color-primary, #8b5cf6);
  color: #fff;
  font-size: 12px;
  cursor: pointer;
}

.submit-feedback:disabled {
  opacity: .45;
  cursor: not-allowed;
}

.feedback-pop-enter-active,
.feedback-pop-leave-active {
  transition: opacity .12s ease, transform .12s ease;
}

.feedback-pop-enter-from,
.feedback-pop-leave-to {
  opacity: 0;
  transform: translateY(4px);
}
</style>
