// Check actual build output and synchronous import closure, not source imports.
import { readFileSync, statSync } from 'node:fs'
import { resolve } from 'node:path'

const root = resolve(import.meta.dirname, '../dist')
const manifest = JSON.parse(readFileSync(resolve(root, '.vite/manifest.json'), 'utf8'))
const size = file => statSync(resolve(root, file)).size
function closure(key, seen = new Set()) {
  if (seen.has(key)) return seen
  seen.add(key)
  for (const imported of manifest[key].imports ?? []) closure(imported, seen)
  return seen
}
const entry = 'src/plugins/adventurer_s1/runtime.ts'
const keys = closure(entry)
const js = new Set([...keys].map(key => manifest[key].file))
const css = new Set([...keys].flatMap(key => manifest[key].css ?? []))
const total = files => [...files].reduce((sum, file) => sum + size(file), 0)
const limits = { js: 700_000, css: 250_000, chunk: 250_000 }
const failures = []
// S1 colors also serve dialogs teleported to body, so they must exist in the
// document's initial stylesheet, independently of any lazy game panel.
const documentCss = (manifest['index.html'].css ?? []).map(file => readFileSync(resolve(root, file), 'utf8')).join('\n')
const rootRules = [...documentCss.matchAll(/:root\s*\{([^}]+)\}/g)].map(match => match[1]).join(';')
for (const token of ['bg', 'surface', 'border', 'text', 'text-secondary', 'primary', 'success', 'danger', 'warning', 'hover']) {
  if (!rootRules.includes(`--color-${token}:`)) failures.push(`Missing global S1 theme token: --color-${token}`)
}
for (const [key, chunk] of Object.entries(manifest)) {
  if (chunk.file.endsWith('.js') && size(chunk.file) > limits.chunk) failures.push(`${key}: JS chunk exceeds ${limits.chunk} bytes`)
}
if (total(js) > limits.js) failures.push('S1 synchronous JavaScript exceeds budget')
if (total(css) > limits.css) failures.push('S1 synchronous CSS exceeds budget')
for (const component of ['MarketPanel', 'CampaignHallPanel', 'BuildingDetailModal', 'AdventurerDetailModal', 'AdminHubPanel']) {
  if ([...keys].some(key => key.includes(`/${component}.vue`))) failures.push(`${component} loaded eagerly`)
}
console.log(`S1 synchronous assets: JS ${total(js)} bytes; CSS ${total(css)} bytes; max JS chunk ${Math.max(...Object.values(manifest).filter(c => c.file.endsWith('.js')).map(c => size(c.file)))} bytes`)
if (failures.length) throw new Error(failures.join('\n'))
