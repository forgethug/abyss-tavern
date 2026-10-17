<template>
  <span
    class="squad-emblem"
    :class="emblemClasses"
    :style="{ '--squad-emblem-size': `${size}px` }"
    :title="title"
    role="img"
    :aria-label="title || '纹章小队徽记'"
  >
    <span class="squad-emblem-pattern" aria-hidden="true"></span>
    <span class="squad-emblem-sigil" aria-hidden="true">
      <component
        :is="sigilComponent"
        v-if="sigilComponent"
        class="squad-emblem-sigil-icon"
      />
      <span v-else>{{ sigilGlyph }}</span>
    </span>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { Component } from 'vue'
import {
  Boat,
  Cloud,
  Compass,
  Diamond,
  Dice,
  Eye,
  Flame,
  Flash,
  Flower,
  Hammer,
  Heart,
  Key,
  Leaf,
  Library,
  Moon,
  MusicalNotes,
  Paw,
  Planet,
  Prism,
  Rose,
  Shield,
  Skull,
  Snow,
  Star,
  Sunny,
  Trophy,
  Water,
  Wine,
} from '@vicons/ionicons5'
import { normalizeSquadEmblem } from '../squadEmblem'

const props = withDefaults(defineProps<{
  emblem: unknown
  size?: number
  title?: string
}>(), {
  size: 32,
  title: '',
})

const safeEmblem = computed(() => normalizeSquadEmblem(props.emblem))
const emblemClasses = computed(() => [
  `frame-${safeEmblem.value.frame}`,
  `pattern-${safeEmblem.value.pattern}`,
  `primary-${safeEmblem.value.primary}`,
  `secondary-${safeEmblem.value.secondary}`,
  `accent-${safeEmblem.value.accent}`,
])

const SIGIL_GLYPHS: Record<string, string> = {
  sword: '⚔',
  tower: '♜',
}
const SIGIL_COMPONENTS: Record<string, Component> = {
  star: Star,
  moon: Moon,
  flame: Flame,
  leaf: Leaf,
  shield: Shield,
  sun: Sunny,
  eye: Eye,
  skull: Skull,
  key: Key,
  paw: Paw,
  rose: Rose,
  lightning: Flash,
  snow: Snow,
  compass: Compass,
  planet: Planet,
  gem: Diamond,
  hammer: Hammer,
  anchor: Boat,
  chalice: Wine,
  dice: Dice,
  book: Library,
  music: MusicalNotes,
  heart: Heart,
  water: Water,
  cloud: Cloud,
  flower: Flower,
  crown: Trophy,
  prism: Prism,
}
const sigilComponent = computed(() => SIGIL_COMPONENTS[safeEmblem.value.sigil] ?? null)
const sigilGlyph = computed(() => SIGIL_GLYPHS[safeEmblem.value.sigil] ?? '✦')
</script>

<style scoped>
.squad-emblem {
  --emblem-primary: #a83a4b;
  --emblem-secondary: #252a31;
  --emblem-accent: #c89a3d;
  --emblem-edge: max(1px, calc(var(--squad-emblem-size) * 0.07));
  position: relative;
  display: inline-flex;
  flex: 0 0 auto;
  width: var(--squad-emblem-size);
  height: var(--squad-emblem-size);
  align-items: center;
  justify-content: center;
  overflow: hidden;
  border: var(--emblem-edge) solid var(--emblem-accent);
  background: var(--emblem-primary);
  box-shadow:
    inset 0 0 0 max(1px, calc(var(--squad-emblem-size) * 0.035)) rgba(255, 255, 255, 0.24),
    0 1px 3px rgba(0, 0, 0, 0.3);
  vertical-align: middle;
}

.frame-round { border-radius: 50%; }
.frame-shield { border-radius: 42% 42% 54% 54% / 24% 24% 76% 76%; }
.frame-diamond { border-radius: 16%; transform: rotate(45deg) scale(0.82); }
.frame-diamond > * { transform: rotate(-45deg); }
.frame-hex { clip-path: polygon(25% 4%, 75% 4%, 98% 50%, 75% 96%, 25% 96%, 2% 50%); }
.frame-square { border-radius: 17%; }
.frame-arch { border-radius: 50% 50% 16% 16% / 38% 38% 15% 15%; }
.frame-crest { clip-path: polygon(50% 2%, 95% 18%, 90% 65%, 50% 98%, 10% 65%, 5% 18%); }
.frame-octagon { clip-path: polygon(30% 2%, 70% 2%, 98% 30%, 98% 70%, 70% 98%, 30% 98%, 2% 70%, 2% 30%); }
.frame-banner { clip-path: polygon(8% 3%, 92% 3%, 92% 78%, 73% 97%, 50% 82%, 27% 97%, 8% 78%); }
.frame-rune {
  border-radius: 50%;
  outline: max(1px, calc(var(--squad-emblem-size) * 0.035)) dashed rgba(255, 255, 255, 0.68);
  outline-offset: calc(var(--squad-emblem-size) * -0.16);
}
.frame-tablet { border-radius: 48% 48% 16% 16% / 25% 25% 14% 14%; }
.frame-petal { border-radius: 62% 38% 58% 42% / 58% 44% 56% 42%; }

.frame-hex,
.frame-crest,
.frame-octagon,
.frame-banner {
  border: 0;
  background: var(--emblem-accent);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
}

.frame-hex .squad-emblem-pattern,
.frame-crest .squad-emblem-pattern,
.frame-octagon .squad-emblem-pattern,
.frame-banner .squad-emblem-pattern {
  inset: var(--emblem-edge);
}

.frame-hex .squad-emblem-pattern {
  clip-path: polygon(25% 4%, 75% 4%, 98% 50%, 75% 96%, 25% 96%, 2% 50%);
}

.frame-crest .squad-emblem-pattern {
  clip-path: polygon(50% 2%, 95% 18%, 90% 65%, 50% 98%, 10% 65%, 5% 18%);
}

.frame-octagon .squad-emblem-pattern {
  clip-path: polygon(30% 2%, 70% 2%, 98% 30%, 98% 70%, 70% 98%, 30% 98%, 2% 70%, 2% 30%);
}

.frame-banner .squad-emblem-pattern {
  clip-path: polygon(8% 3%, 92% 3%, 92% 78%, 73% 97%, 50% 82%, 27% 97%, 8% 78%);
}

.squad-emblem-pattern {
  position: absolute;
  inset: 0;
  background: var(--emblem-primary);
}

.pattern-solid .squad-emblem-pattern {
  background: var(--emblem-primary);
}

.pattern-split .squad-emblem-pattern {
  background: linear-gradient(90deg, var(--emblem-primary) 0 50%, var(--emblem-secondary) 50%);
}

.pattern-chevron .squad-emblem-pattern {
  background:
    linear-gradient(145deg, transparent 0 38%, var(--emblem-secondary) 39% 61%, transparent 62%),
    var(--emblem-primary);
}

.pattern-rays .squad-emblem-pattern {
  background:
    conic-gradient(
      from 22.5deg,
      var(--emblem-primary) 0 12.5%,
      var(--emblem-secondary) 12.5% 25%,
      var(--emblem-primary) 25% 37.5%,
      var(--emblem-secondary) 37.5% 50%,
      var(--emblem-primary) 50% 62.5%,
      var(--emblem-secondary) 62.5% 75%,
      var(--emblem-primary) 75% 87.5%,
      var(--emblem-secondary) 87.5% 100%
    );
}

.pattern-diagonal .squad-emblem-pattern {
  background: linear-gradient(135deg, var(--emblem-primary) 0 49%, var(--emblem-secondary) 50% 100%);
}

.pattern-quarters .squad-emblem-pattern {
  background:
    conic-gradient(
      from 90deg,
      var(--emblem-primary) 0 25%,
      var(--emblem-secondary) 25% 50%,
      var(--emblem-primary) 50% 75%,
      var(--emblem-secondary) 75% 100%
    );
}

.pattern-cross .squad-emblem-pattern {
  background:
    linear-gradient(90deg, transparent 0 38%, var(--emblem-secondary) 39% 61%, transparent 62%),
    linear-gradient(0deg, transparent 0 38%, var(--emblem-secondary) 39% 61%, transparent 62%),
    var(--emblem-primary);
}

.pattern-stripes .squad-emblem-pattern {
  background:
    repeating-linear-gradient(
      120deg,
      var(--emblem-primary) 0 18%,
      var(--emblem-secondary) 18% 36%
    );
}

.pattern-rings .squad-emblem-pattern {
  background:
    repeating-radial-gradient(
      circle,
      var(--emblem-primary) 0 17%,
      var(--emblem-secondary) 18% 32%
    );
}

.pattern-checker .squad-emblem-pattern {
  background:
    conic-gradient(
      var(--emblem-secondary) 0 25%,
      var(--emblem-primary) 0 50%,
      var(--emblem-secondary) 0 75%,
      var(--emblem-primary) 0
    ) 0 0 / 50% 50%;
}

.pattern-horizon .squad-emblem-pattern {
  background: linear-gradient(0deg, var(--emblem-secondary) 0 43%, var(--emblem-primary) 44% 100%);
}

.pattern-halo .squad-emblem-pattern {
  background:
    radial-gradient(
      circle,
      var(--emblem-secondary) 0 25%,
      var(--emblem-primary) 26% 49%,
      var(--emblem-secondary) 50% 68%,
      var(--emblem-primary) 69% 100%
    );
}

.squad-emblem-sigil {
  position: relative;
  z-index: 1;
  display: flex;
  width: 74%;
  height: 74%;
  align-items: center;
  justify-content: center;
  color: var(--emblem-accent);
  font-family: Georgia, 'Times New Roman', serif;
  font-size: calc(var(--squad-emblem-size) * 0.58);
  font-weight: 800;
  line-height: 1;
  text-shadow:
    0 1px 0 rgba(0, 0, 0, 0.65),
    0 0 2px rgba(0, 0, 0, 0.45);
}

.squad-emblem-sigil > :deep(svg) {
  width: 100%;
  height: 100%;
  fill: currentColor;
}

.primary-crimson { --emblem-primary: #a83a4b; }
.primary-azure { --emblem-primary: #3674a5; }
.primary-emerald { --emblem-primary: #3d7f66; }
.primary-violet { --emblem-primary: #76558f; }
.primary-gold { --emblem-primary: #c89a3d; }
.primary-ivory { --emblem-primary: #e7dfcc; }
.primary-obsidian { --emblem-primary: #252a31; }
.primary-copper { --emblem-primary: #a96845; }
.primary-sapphire { --emblem-primary: #294a78; }
.primary-teal { --emblem-primary: #2d7c79; }
.primary-jade { --emblem-primary: #6f9d64; }
.primary-rose { --emblem-primary: #b65a78; }
.primary-amber { --emblem-primary: #cf7d32; }
.primary-silver { --emblem-primary: #aeb6c2; }
.primary-umber { --emblem-primary: #6a4c3b; }
.primary-midnight { --emblem-primary: #2b294f; }
.primary-coral { --emblem-primary: #d3685c; }
.primary-lavender { --emblem-primary: #9a79b6; }

.secondary-crimson { --emblem-secondary: #a83a4b; }
.secondary-azure { --emblem-secondary: #3674a5; }
.secondary-emerald { --emblem-secondary: #3d7f66; }
.secondary-violet { --emblem-secondary: #76558f; }
.secondary-gold { --emblem-secondary: #c89a3d; }
.secondary-ivory { --emblem-secondary: #e7dfcc; }
.secondary-obsidian { --emblem-secondary: #252a31; }
.secondary-copper { --emblem-secondary: #a96845; }
.secondary-sapphire { --emblem-secondary: #294a78; }
.secondary-teal { --emblem-secondary: #2d7c79; }
.secondary-jade { --emblem-secondary: #6f9d64; }
.secondary-rose { --emblem-secondary: #b65a78; }
.secondary-amber { --emblem-secondary: #cf7d32; }
.secondary-silver { --emblem-secondary: #aeb6c2; }
.secondary-umber { --emblem-secondary: #6a4c3b; }
.secondary-midnight { --emblem-secondary: #2b294f; }
.secondary-coral { --emblem-secondary: #d3685c; }
.secondary-lavender { --emblem-secondary: #9a79b6; }

.accent-crimson { --emblem-accent: #a83a4b; }
.accent-azure { --emblem-accent: #3674a5; }
.accent-emerald { --emblem-accent: #3d7f66; }
.accent-violet { --emblem-accent: #76558f; }
.accent-gold { --emblem-accent: #c89a3d; }
.accent-ivory { --emblem-accent: #e7dfcc; }
.accent-obsidian { --emblem-accent: #252a31; }
.accent-copper { --emblem-accent: #a96845; }
.accent-sapphire { --emblem-accent: #294a78; }
.accent-teal { --emblem-accent: #2d7c79; }
.accent-jade { --emblem-accent: #6f9d64; }
.accent-rose { --emblem-accent: #b65a78; }
.accent-amber { --emblem-accent: #cf7d32; }
.accent-silver { --emblem-accent: #aeb6c2; }
.accent-umber { --emblem-accent: #6a4c3b; }
.accent-midnight { --emblem-accent: #2b294f; }
.accent-coral { --emblem-accent: #d3685c; }
.accent-lavender { --emblem-accent: #9a79b6; }
</style>
