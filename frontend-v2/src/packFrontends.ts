import type { Component } from 'vue'

// Only the game included in this distribution is compiled into the website.
export const packFrontends: Record<string, () => Promise<{ default: Component }>> = {
  '../plugins/adventurer_s1/runtime.ts': () => import('./plugins/adventurer_s1/runtime'),
}
