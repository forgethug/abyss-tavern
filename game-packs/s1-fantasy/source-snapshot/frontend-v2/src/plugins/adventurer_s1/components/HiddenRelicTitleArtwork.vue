<template>
  <span class="relic-artwork" :class="[`relic-${variant}`, { 'is-inactive': !active }]" aria-hidden="true">
    <svg class="relic-outline" viewBox="0 0 100 100" preserveAspectRatio="none">
      <path class="relic-line relic-brass" d="M12 1.5 H88 L98.5 10 V90 L88 98.5 H12 L1.5 90 V10 Z" />
      <path class="relic-line relic-engraving" d="M15 3.3 H85 M96.7 14 V40 L95 43 V57 L96.7 60 V86 M85 96.7 H15 M3.3 86 V60 L5 57 V43 L3.3 40 V14" />
      <path class="relic-line relic-trail" pathLength="100" d="M12 1.5 H88 L98.5 10 V90 L88 98.5 H12 L1.5 90 V10 Z" />
    </svg>

    <svg v-for="corner in ['tl', 'tr', 'bl', 'br']" :key="corner" class="relic-fixed relic-corner" :class="`relic-corner-${corner}`" viewBox="0 0 44 44">
      <path class="relic-metal" d="M3 34 V14 L14 3 H34 L29 8 H17 L8 17 V29 Z" />
      <path class="relic-line relic-brass" d="M5 34 V15 L15 5 H34 M12 32 V20 L20 12 H32" />
      <path class="relic-line relic-engraving" d="M17 26 V20 H26 M22 5 V9 M5 22 H9" />
      <path class="relic-epic" d="M12 8 L16 12 L12 16 L8 12 Z" />
    </svg>

    <svg class="relic-fixed relic-vault" viewBox="0 0 180 66">
      <path class="relic-metal" d="M18 27 H37 L48 18 H66 L76 8 H104 L114 18 H132 L143 27 H162 L155 42 H139 L128 51 H110 L100 59 H80 L70 51 H52 L41 42 H25 Z" />
      <path class="relic-line relic-brass" d="M18 27 H37 L48 18 H66 M114 18 H132 L143 27 H162 M25 42 H41 L52 51 H70 M110 51 H128 L139 42 H155" />
      <path class="relic-line relic-engraving" d="M31 32 H46 L56 24 H66 M114 24 H124 L134 32 H149 M49 39 H61 M119 39 H131" />
      <path class="relic-line relic-brass" d="M70 50 V24 L90 8 L110 24 V50 L90 61 Z" />
      <path class="relic-door" d="M78 47 V27 L90 17 L102 27 V47 Z" />
      <path class="relic-line relic-engraving" d="M82 43 V30 L90 24 L98 30 V43 M90 25 V43 M75 51 H105" />
      <path class="relic-line relic-brass" d="M84 34 H96" />
      <g class="relic-seals">
        <path class="relic-legendary" d="M58 14 L65 23 L58 32 L51 23 Z M122 14 L129 23 L122 32 L115 23 Z M90 25 L96 34 L90 43 L84 34 Z M76 3 L82 10 L76 17 L70 10 Z M104 3 L110 10 L104 17 L98 10 Z" />
        <path class="relic-facet" d="M58 16 V30 L53 23 Z M122 16 V30 L117 23 Z M90 27 V41 L86 34 Z M76 5 V15 L72 10 Z M104 5 V15 L100 10 Z" />
        <path class="relic-epic" d="M37 29 L42 35 L37 41 L32 35 Z M143 29 L148 35 L143 41 L138 35 Z M66 40 L71 46 L66 52 L61 46 Z M114 40 L119 46 L114 52 L109 46 Z M81 49 L85 54 L81 59 L77 54 Z M99 49 L103 54 L99 59 L95 54 Z" />
      </g>
    </svg>

    <svg class="relic-fixed relic-key" viewBox="0 0 120 30">
      <path class="relic-line relic-engraving" d="M4 15 H33 M86 15 H116 M12 11 H25 M96 19 H108" />
      <path class="relic-line relic-brass" d="M45 9 L51 15 L45 21 L39 15 Z M51 15 H78 V20 H72 V15 M61 15 V10 H66" />
      <path class="relic-epic" d="M45 12 L48 15 L45 18 L42 15 Z" />
      <path class="relic-line relic-brass" d="M30 15 L26 19 M90 15 L94 11" />
    </svg>
  </span>
</template>

<script setup lang="ts">
withDefaults(defineProps<{ variant?: 'card' | 'detail'; active?: boolean }>(), { variant: 'card', active: true })
</script>

<style scoped>
.relic-artwork {
  --relic-brass: #d1af71;
  --relic-ink: #8b704f;
  --relic-metal: #29232e;
  --relic-violet: #b99ae8;
  --relic-amber: #efb365;
  position: absolute;
  inset: 0;
  pointer-events: none;
}
.relic-outline, .relic-fixed { position: absolute; display: block; overflow: visible; }
.relic-outline { inset: 0; width: 100%; height: 100%; }
.relic-line { fill: none; stroke-linecap: round; stroke-linejoin: round; vector-effect: non-scaling-stroke; }
.relic-brass { stroke: var(--relic-brass); stroke-width: 1.3; }
.relic-engraving { stroke: var(--relic-ink); stroke-width: .9; }
.relic-metal { fill: var(--relic-metal); stroke: var(--relic-ink); stroke-width: .8; }
.relic-epic { fill: var(--relic-violet); stroke: var(--relic-brass); stroke-width: .8; }
.relic-legendary { fill: var(--relic-amber); stroke: var(--relic-brass); stroke-width: 1; }
.relic-facet { fill: #fff0c9; opacity: .5; }
.relic-door { fill: var(--color-surface, #161b22); stroke: var(--relic-violet); stroke-width: 1; }
.relic-vault { top: -3px; right: 12px; width: 146px; height: 54px; }
.relic-corner { width: 34px; height: 34px; }
.relic-corner-tl { top: -2px; left: -2px; }
.relic-corner-tr { top: -2px; right: -2px; transform: rotate(90deg); }
.relic-corner-br { bottom: -2px; right: -2px; transform: rotate(180deg); }
.relic-corner-bl { bottom: -2px; left: -2px; transform: rotate(-90deg); }
.relic-key { bottom: -5px; left: calc(50% - 52px); width: 104px; height: 26px; }
.relic-trail { stroke: var(--relic-violet); stroke-width: 1.6; stroke-dasharray: 3 97; animation: relic-trace 18s linear infinite; }
.relic-seals { animation: relic-glimmer 8s ease-in-out infinite; }
.is-inactive * { animation-play-state: paused !important; }
.relic-detail .relic-vault { top: 5px; right: 50px; width: 140px; height: 52px; }
.relic-detail .relic-key { bottom: 1px; }
:global([data-theme="light"] .relic-artwork) {
  --relic-brass: #90611c;
  --relic-ink: #ad9676;
  --relic-metal: #f7efdf;
  --relic-violet: #7950ae;
  --relic-amber: #c67a23;
}
@keyframes relic-trace { to { stroke-dashoffset: -100; } }
@keyframes relic-glimmer { 0%, 100% { opacity: .74; } 50% { opacity: 1; } }
@media (max-width: 640px) {
  .relic-detail .relic-vault { top: 7px; right: 42px; width: 102px; height: 42px; }
}
@media (prefers-reduced-motion: reduce) {
  .relic-trail, .relic-seals { animation: none; }
}
</style>
