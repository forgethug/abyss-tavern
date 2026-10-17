<template>
  <aside v-if="!acknowledged" class="beta-notice" aria-label="抢先体验说明">
    <span class="beta-notice-icon" aria-hidden="true">🧪</span>
    <div class="beta-notice-body">
      <strong>抢先体验 · 测试版本</strong>
      <p>本玩法处于抢先体验阶段，<b>剧情走向、数值平衡、功能完善度均未定稿</b>。探险剧情完全由 AI 实时生成，可能出现奇怪的展开、不合理的判定甚至恶性 Bug 导致冒险者意外死亡。</p>
      <p>请抱着<b>「看戏」</b>的心态游玩——AI 的脑洞你永远猜不到，遇到离谱的情况欢迎反馈，但别太生气，毕竟连我们也不知道它下一步会写什么。</p>
      <p>本玩法由 AI 驱动，开发资源有限，<b>请勿开小号</b>。系统会检测多账号行为，多号玩家可能会被加入更低优先级的排队列表。</p>
      <button type="button" class="beta-notice-confirm" @click="acknowledge">我已知晓，不再提示</button>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { ref } from 'vue'

// 两个区服共用同一份测试说明；说明有实质变化时升级版本，重新请求确认。
const STORAGE_KEY = 'adventurer_s1_beta_notice_ack_v1'
const emit = defineEmits<{ acknowledge: [] }>()

function readAcknowledgement(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === '1'
  } catch {
    return false
  }
}

const acknowledged = ref(readAcknowledgement())

function acknowledge() {
  acknowledged.value = true
  try {
    localStorage.setItem(STORAGE_KEY, '1')
  } catch {
    // 隐私模式或存储不可用时，仍允许关闭本次显示。
  }
  emit('acknowledge')
}
</script>

<style scoped>
.beta-notice {
  display: flex;
  gap: 12px;
  padding: 14px;
  margin-bottom: 14px;
  border: 1px solid var(--color-border);
  border-color: color-mix(in srgb, #e6a817 30%, var(--color-border));
  border-radius: 12px;
  background: var(--color-surface);
  background: color-mix(in srgb, #e6a817 8%, var(--color-surface));
  color: var(--color-text-secondary);
  font-size: 13px;
  line-height: 1.7;
}
.beta-notice-icon { flex: none; font-size: 22px; }
.beta-notice-body { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.beta-notice-body strong { display: block; color: var(--color-text); font-size: 14px; }
.beta-notice-body p { margin: 6px 0; }
.beta-notice-body b { color: var(--color-text); }
.beta-notice-confirm {
  min-height: 34px;
  margin-top: 8px;
  padding: 6px 12px;
  border: 1px solid var(--color-border);
  border-radius: 7px;
  background: var(--color-surface);
  color: var(--color-text);
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}
.beta-notice-confirm:focus-visible { outline: 2px solid var(--color-primary); outline-offset: 2px; }
@media (max-width: 640px) {
  .beta-notice { gap: 8px; padding: 12px; font-size: 12px; line-height: 1.65; }
  .beta-notice-icon { font-size: 18px; }
  .beta-notice-body strong { font-size: 13px; }
  .beta-notice-confirm { display: block; margin-left: auto; font-size: 12px; }
}
</style>
