<template>
  <span
    ref="artworkRef"
    class="title-frame-artwork"
    :class="[`is-${kind}`, `is-${variant}`, { 'is-viewport-inactive': !isViewportActive }]"
  >
    <template v-if="kind === 'fate'">
      <svg class="frame-stretch" viewBox="0 0 100 100" preserveAspectRatio="none">
        <path class="fate-line fate-line-gold" d="M12 1.4 H75 Q82 1.4 86 5.2 Q90 9 98.6 10 V38 Q96 43 98.6 48 V90 Q98.6 98.6 90 98.6 H58 Q54 98.6 50 95.6 Q46 98.6 42 98.6 H10 Q1.4 98.6 1.4 90 V62 Q4 57 1.4 52 V10 Q1.4 1.4 12 1.4 Z" />
        <path class="fate-line fate-line-violet" d="M16 3.8 H73 Q80 3.8 84 7.4 M96.2 14 V37 Q93.7 43 96.2 49 V86 M86 96.2 H60 Q54 96.2 50 92 Q46 96.2 40 96.2 H14 M3.8 86 V65 Q6.3 58 3.8 51 V14" />
        <path class="fate-line fate-line-filigree" d="M24 1.4 C28 8 34 8 38 1.4 M62 1.4 C66 8 72 8 76 1.4 M98.6 27 C91 31 91 36 98.6 40 M98.6 64 C91 68 91 73 98.6 77 M76 98.6 C72 91 66 91 62 98.6 M38 98.6 C34 91 28 91 24 98.6 M1.4 77 C9 73 9 68 1.4 64 M1.4 40 C9 36 9 31 1.4 27" />
        <path class="fate-rail-glint" d="M12 1.4 H75 Q82 1.4 86 5.2 Q90 9 98.6 10 V38 Q96 43 98.6 48 V90 Q98.6 98.6 90 98.6 H58 Q54 98.6 50 95.6 Q46 98.6 42 98.6 H10 Q1.4 98.6 1.4 90 V62 Q4 57 1.4 52 V10 Q1.4 1.4 12 1.4 Z" />
      </svg>

      <svg class="fixed-art fate-flourish fate-flourish-tl" viewBox="0 0 120 120">
        <path class="fate-line fate-line-gold" d="M7 82 C10 39 31 12 78 7 C62 17 56 31 59 47 C43 37 24 46 20 64 C33 53 50 54 61 67 C45 63 31 72 26 92" />
        <path class="fate-line fate-line-violet" d="M15 90 C24 68 39 55 61 50 C43 45 33 30 36 14 M31 76 C43 70 54 72 64 82" />
        <path class="fate-leaf" d="M54 24 Q69 15 78 8 Q75 25 60 31 Z M26 53 Q13 45 8 34 Q24 36 33 47 Z M45 72 Q57 65 70 69 Q59 80 47 82 Z" />
      </svg>
      <svg class="fixed-art fate-flourish fate-flourish-br" viewBox="0 0 120 120">
        <path class="fate-line fate-line-gold" d="M7 82 C10 39 31 12 78 7 C62 17 56 31 59 47 C43 37 24 46 20 64 C33 53 50 54 61 67 C45 63 31 72 26 92" />
        <path class="fate-line fate-line-violet" d="M15 90 C24 68 39 55 61 50 C43 45 33 30 36 14 M31 76 C43 70 54 72 64 82" />
        <path class="fate-leaf" d="M54 24 Q69 15 78 8 Q75 25 60 31 Z M26 53 Q13 45 8 34 Q24 36 33 47 Z M45 72 Q57 65 70 69 Q59 80 47 82 Z" />
      </svg>
      <svg class="fixed-art fate-crown" viewBox="0 0 240 74">
        <path class="fate-crown-wash" d="M12 60 C44 57 49 34 79 35 C96 35 107 45 119 55 C132 42 142 24 165 22 C190 20 199 46 228 55 L228 68 L12 68 Z" />
        <path class="fate-line fate-line-gold" d="M8 63 C37 62 47 48 58 37 C69 26 84 27 94 37 C101 44 108 53 119 58 C130 49 136 32 150 24 C169 13 187 23 193 39 C198 51 211 59 232 61" />
        <path class="fate-line fate-line-violet" d="M21 68 C48 65 52 51 65 45 C80 37 93 45 103 55 M135 50 C145 35 157 29 170 31 C186 33 190 49 202 57 C211 63 221 66 235 66" />
        <path class="fate-leaf" d="M46 51 Q34 39 37 26 Q52 35 54 47 Z M72 35 Q70 20 81 12 Q87 28 78 38 Z M101 48 Q103 32 114 27 Q116 43 107 53 Z M193 44 Q199 28 212 25 Q209 42 198 51 Z M215 57 Q225 43 237 44 Q231 58 219 64 Z" />
        <g class="fate-orbit fate-orbit-a">
          <ellipse class="fate-line fate-line-gold" cx="160" cy="28" rx="28" ry="12" transform="rotate(-17 160 28)" />
        </g>
        <g class="fate-orbit fate-orbit-b">
          <ellipse class="fate-line fate-line-violet" cx="160" cy="28" rx="12" ry="28" transform="rotate(28 160 28)" />
        </g>
        <circle class="fate-disc" cx="160" cy="28" r="20" />
        <path class="fate-star fate-star-pulse" d="M160 14 L164 23 L174 24 L166 31 L169 41 L160 35 L151 41 L154 31 L146 24 L156 23 Z" />
        <circle class="fate-core fate-core-pulse" cx="160" cy="28" r="3.8" />
        <path class="fate-crown-glint" d="M8 63 C37 62 47 48 58 37 C69 26 84 27 94 37 C101 44 108 53 119 58 C130 49 136 32 150 24 C169 13 187 23 193 39 C198 51 211 59 232 61" />
        <circle class="fate-spark spark-a" cx="118" cy="57" r="1.8" />
        <circle class="fate-spark spark-b" cx="202" cy="54" r="1.4" />
        <circle class="fate-spark spark-c" cx="61" cy="36" r="1.2" />
      </svg>
      <svg class="fixed-art fate-road" viewBox="0 0 150 110">
        <path class="fate-line fate-line-gold" d="M8 94 C28 90 27 65 47 62 C67 59 60 35 82 31 C103 27 105 12 141 8" />
        <path class="fate-line fate-line-violet" d="M17 102 C37 91 38 75 56 72 C77 68 72 45 94 40 C113 36 120 22 143 20" />
        <path class="fate-line fate-line-gold" d="M22 85 Q12 70 25 59 Q30 76 22 85 Z M54 57 Q47 43 58 34 Q63 49 54 57 Z M93 28 Q88 16 99 10 Q102 23 93 28 Z" />
      </svg>
    </template>

    <HiddenRelicTitleArtwork v-else-if="kind === 'hidden_relic'" :variant="variant" :active="isViewportActive" />

    <template v-else>
      <svg class="frame-stretch" viewBox="0 0 100 100" preserveAspectRatio="none">
        <path class="companion-line companion-line-emerald" d="M9 1.4 H91 Q98.6 1.4 98.6 9 V91 Q98.6 98.6 91 98.6 H9 Q1.4 98.6 1.4 91 V9 Q1.4 1.4 9 1.4 Z" />
        <path class="companion-line companion-line-gold" d="M14 3.8 H86 Q96.2 3.8 96.2 14 V86 Q96.2 96.2 86 96.2 H14 Q3.8 96.2 3.8 86 V14 Q3.8 3.8 14 3.8 Z" />
        <path class="companion-path-glint" d="M9 1.4 H91 Q98.6 1.4 98.6 9 V91 Q98.6 98.6 91 98.6 H9 Q1.4 98.6 1.4 91 V9 Q1.4 1.4 9 1.4 Z" />
      </svg>

      <svg class="fixed-art companion-lantern" viewBox="0 0 170 76">
        <path class="companion-branch" d="M8 58 C32 55 38 38 58 38 C73 38 76 50 86 58 C97 47 102 29 121 27 C139 25 146 43 162 49" />
        <path class="companion-branch companion-branch-soft" d="M18 67 C39 63 45 50 62 49 M103 49 C113 36 127 34 145 46" />
        <path class="companion-lantern-roof" d="M78 19 L86 10 L94 19 L101 24 H71 Z" />
        <path class="companion-lantern-body" d="M75 24 H97 L94 52 H78 Z" />
        <path class="companion-lantern-core" d="M82 29 H90 L92 45 H80 Z" />
        <path class="companion-lantern-ring" d="M86 10 C77 10 69 17 69 27 C69 39 77 48 86 58 C95 48 103 39 103 27 C103 17 95 10 86 10 Z" />
        <circle class="companion-waypoint point-a" cx="40" cy="50" r="3" />
        <circle class="companion-waypoint point-b" cx="121" cy="28" r="3" />
        <circle class="companion-waypoint point-c" cx="151" cy="45" r="2.4" />
      </svg>

      <svg class="fixed-art companion-roadmap" viewBox="0 0 150 120">
        <path class="companion-line companion-line-emerald" d="M8 105 C26 91 21 70 43 65 C65 60 52 37 76 34 C100 31 101 12 139 8" />
        <path class="companion-line companion-line-gold companion-dash" d="M18 112 C38 98 34 79 54 74 C76 68 67 49 91 44 C112 40 119 23 143 18" />
        <circle class="companion-waypoint point-a" cx="43" cy="65" r="3.2" />
        <circle class="companion-waypoint point-b" cx="76" cy="34" r="3.2" />
        <circle class="companion-waypoint point-c" cx="139" cy="8" r="3.2" />
      </svg>

      <svg class="fixed-art companion-knot companion-knot-tl" viewBox="0 0 82 82">
        <path class="companion-line companion-line-emerald" d="M5 67 C9 31 31 9 67 5 C53 16 48 29 52 42 C39 33 25 39 20 53 C33 47 44 51 51 62" />
        <path class="companion-line companion-line-gold companion-dash" d="M12 72 C24 54 37 45 57 43 M25 22 C30 34 39 41 52 44" />
      </svg>
      <svg class="fixed-art companion-knot companion-knot-br" viewBox="0 0 82 82">
        <path class="companion-line companion-line-emerald" d="M5 67 C9 31 31 9 67 5 C53 16 48 29 52 42 C39 33 25 39 20 53 C33 47 44 51 51 62" />
        <path class="companion-line companion-line-gold companion-dash" d="M12 72 C24 54 37 45 57 43 M25 22 C30 34 39 41 52 44" />
      </svg>
    </template>
  </span>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import HiddenRelicTitleArtwork from './HiddenRelicTitleArtwork.vue'
import { useViewportActivity } from '../composables/useViewportActivity'

type TitleFrameKind = 'fate' | 'fate_companion' | 'hidden_relic'

withDefaults(defineProps<{
  kind: TitleFrameKind
  variant?: 'card' | 'detail'
}>(), {
  variant: 'card',
})

const artworkRef = ref<HTMLElement | null>(null)
const { isViewportActive } = useViewportActivity(artworkRef)
</script>

<style scoped lang="scss">
.title-frame-artwork,
.frame-stretch,
.fixed-art {
  position: absolute;
  display: block;
  pointer-events: none;
}

.title-frame-artwork { inset: 0; }
.title-frame-artwork.is-viewport-inactive,
.title-frame-artwork.is-viewport-inactive * {
  animation-play-state: paused !important;
}
.frame-stretch { inset: 0; width: 100%; height: 100%; overflow: visible; }
.fate-line,
.companion-line {
  fill: none;
  vector-effect: non-scaling-stroke;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.fate-line-gold { stroke: #e8c979; stroke-width: 2.4; filter: drop-shadow(0 0 4px rgba(232, 201, 121, .52)); }
.fate-line-violet { stroke: #8065c4; stroke-width: 1.35; opacity: .94; }
.fate-line-filigree { stroke: #f3dfa4; stroke-width: 1.2; opacity: .78; }
.fate-leaf { fill: rgba(154, 131, 215, .12); stroke: #e8c979; stroke-width: 1.5; stroke-linejoin: round; }
.fate-crown-wash { fill: rgba(115, 86, 175, .055); stroke: none; }
.fate-disc { fill: rgba(154, 131, 215, .04); stroke: #8065c4; stroke-width: 1.7; }
.fate-star { fill: rgba(232, 201, 121, .14); stroke: #f3dfa4; stroke-width: 1.7; stroke-linejoin: round; }
.fate-core { fill: #9a83d7; stroke: #fff0bb; stroke-width: 1.5; filter: drop-shadow(0 0 5px rgba(232, 201, 121, .82)); }
.fate-rail-glint,
.fate-crown-glint {
  fill: none;
  stroke: rgba(255, 244, 199, .95);
  stroke-width: 2.1;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-dasharray: 9 260;
  vector-effect: non-scaling-stroke;
  animation: fate-glint 8s linear infinite;
}
.fate-crown-glint { animation-duration: 6.5s; }
.fate-orbit { transform-box: fill-box; transform-origin: center; }
.fate-orbit-a { animation: fate-orbit 12s linear infinite; }
.fate-orbit-b { animation: fate-orbit-reverse 16s linear infinite; }
.fate-orbit .fate-line-gold { filter: none; }
.fate-star-pulse,
.fate-core-pulse { opacity: .9; }
.fate-spark { fill: #fff0bb; opacity: .58; filter: drop-shadow(0 0 3px rgba(232, 201, 121, .68)); }

.fate-flourish {
  width: 64px;
  height: 64px;
  clip-path: polygon(0 0, 100% 0, 100% 32%, 32% 32%, 32% 100%, 0 100%);
}
.fate-flourish-tl { top: -4px; left: -4px; }
.fate-flourish-br { right: -4px; bottom: -4px; transform: rotate(180deg); }
.fate-crown { top: -7px; left: 50%; width: min(76%, 208px); transform: translateX(-50%); }
.fate-road { bottom: -49px; left: 12%; width: 94px; height: 70px; }
.is-card .fate-road {
  bottom: -8px;
  left: 8%;
  width: 82px;
  height: 58px;
}
.is-detail .fate-flourish { width: 96px; height: 96px; }
.is-detail .fate-crown {
  top: 2px;
  right: 72px;
  left: auto;
  width: 210px;
  transform: none;
}
.is-detail .fate-road { bottom: -78px; width: 142px; height: 104px; }

.companion-line-emerald {
  stroke: #54d3b1;
  stroke-width: 2.35;
  filter: drop-shadow(0 0 4px rgba(84, 211, 177, .62));
}
.companion-line-gold { stroke: #f0c25c; stroke-width: 1.45; opacity: .94; }
.companion-dash { stroke-dasharray: 6 5; filter: none; animation: companion-route-flow 5s linear infinite; }
.companion-path-glint {
  fill: none;
  stroke: rgba(231, 255, 245, .96);
  stroke-width: 2;
  stroke-dasharray: 8 245;
  vector-effect: non-scaling-stroke;
  animation: companion-border-flow 7s linear infinite;
}
.companion-branch,
.companion-branch-soft {
  fill: none;
  stroke: #54d3b1;
  stroke-width: 1.8;
  stroke-linecap: round;
  filter: drop-shadow(0 0 4px rgba(84, 211, 177, .58));
}
.companion-branch-soft { stroke: #f0c25c; stroke-width: 1.2; opacity: .8; }
.companion-lantern-roof,
.companion-lantern-body,
.companion-lantern-ring {
  fill: rgba(16, 49, 44, .88);
  stroke: #f0c25c;
  stroke-width: 1.5;
}
.companion-lantern-ring { fill: none; stroke: rgba(84, 211, 177, .7); stroke-dasharray: 5 4; animation: companion-lantern-ring 10s linear infinite; transform-box: fill-box; transform-origin: center; }
.companion-lantern-core {
  fill: #ffe39a;
  stroke: #fff5cc;
  stroke-width: 1;
  opacity: .9;
  filter: drop-shadow(0 0 5px rgba(240, 194, 92, .72));
}
.companion-waypoint {
  fill: #f0c25c;
  stroke: #dcfff6;
  stroke-width: 1;
  opacity: .8;
  filter: drop-shadow(0 0 3px rgba(84, 211, 177, .68));
}
.companion-lantern { top: -5px; left: 50%; width: min(68%, 170px); transform: translateX(-50%); }
.companion-roadmap { right: 5%; bottom: -9px; width: 92px; height: 72px; }
.companion-knot { width: 70px; height: 70px; clip-path: polygon(0 0, 100% 0, 100% 30%, 30% 30%, 30% 100%, 0 100%); }
.companion-knot-tl { top: -4px; left: -4px; }
.companion-knot-br { right: -4px; bottom: -4px; transform: rotate(180deg); }
.is-detail .companion-lantern { top: 2px; right: 72px; left: auto; width: 205px; transform: none; }
.is-detail .companion-roadmap { right: 8%; bottom: -48px; width: 142px; height: 110px; }
.is-detail .companion-knot { width: 102px; height: 102px; }

@media (max-width: 640px) {

  .is-detail .fate-crown {
    top: 4px;
    right: 46px;
    width: 138px;
  }
}
@keyframes fate-glint { to { stroke-dashoffset: -269; } }
@keyframes fate-orbit { to { transform: rotate(360deg); } }
@keyframes fate-orbit-reverse { to { transform: rotate(-360deg); } }
@keyframes companion-route-flow { to { stroke-dashoffset: -44; } }
@keyframes companion-border-flow { to { stroke-dashoffset: -253; } }
@keyframes companion-lantern-ring { to { transform: rotate(360deg); } }

[data-theme="light"] .fate-line-gold { stroke: #a77408; filter: drop-shadow(0 1px 0 rgba(255, 255, 255, .9)); }
[data-theme="light"] .fate-line-violet { stroke: #5d3a9a; opacity: 1; }
[data-theme="light"] .fate-line-filigree { stroke: #8c6207; opacity: .86; }
[data-theme="light"] .fate-leaf { fill: rgba(93, 58, 154, .08); stroke: #9a6905; }
[data-theme="light"] .fate-crown-wash { fill: rgba(93, 58, 154, .045); }
[data-theme="light"] .fate-disc { fill: rgba(255, 255, 255, .56); stroke: #5d3a9a; }
[data-theme="light"] .fate-star { fill: rgba(167, 116, 8, .12); stroke: #7c5200; }
[data-theme="light"] .fate-core { fill: #6942a6; stroke: #9a6905; filter: drop-shadow(0 0 3px rgba(93, 58, 154, .45)); }
[data-theme="light"] .fate-rail-glint,
[data-theme="light"] .fate-crown-glint { stroke: #f5cb63; }
[data-theme="light"] .fate-orbit .fate-line-gold { filter: none; }
[data-theme="light"] .fate-spark { fill: #6b4300; filter: drop-shadow(0 0 2px rgba(255, 214, 112, .8)); }
[data-theme="light"] .companion-line-emerald,
[data-theme="light"] .companion-branch { stroke: #137a67; filter: drop-shadow(0 1px 0 rgba(255, 255, 255, .88)); }
[data-theme="light"] .companion-line-gold,
[data-theme="light"] .companion-branch-soft { stroke: #9a6800; }
[data-theme="light"] .companion-lantern-roof,
[data-theme="light"] .companion-lantern-body { fill: rgba(245, 255, 251, .92); stroke: #9a6800; }
[data-theme="light"] .companion-lantern-ring { stroke: #137a67; }
[data-theme="light"] .companion-lantern-core { fill: #d69a17; stroke: #7a5000; filter: drop-shadow(0 0 3px rgba(214, 154, 23, .58)); }
[data-theme="light"] .companion-waypoint { fill: #9a6800; stroke: #137a67; filter: drop-shadow(0 0 2px rgba(19, 122, 103, .52)); }
[data-theme="light"] .companion-path-glint { stroke: #d69a17; }
[data-theme="light"] .companion-dash { filter: none; }

@media (prefers-reduced-motion: reduce) {
  .fate-rail-glint,
  .fate-crown-glint,
  .fate-orbit,
  .fate-star-pulse,
  .fate-core-pulse,
  .fate-spark,
  .companion-dash,
  .companion-path-glint,
  .companion-lantern-ring,
  .companion-lantern-core,
  .companion-waypoint{ animation: none; }
  .fate-spark { opacity: .55; }
}
</style>
