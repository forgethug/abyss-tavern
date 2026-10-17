import { readFileSync, readdirSync, writeFileSync, existsSync } from 'node:fs'
import { resolve, join } from 'node:path'

const root = process.cwd()
const lock = JSON.parse(readFileSync(join(root, 'package-lock.json'), 'utf8'))
const bundled = JSON.parse(readFileSync(join(root, 'dist/third-party-packages.json'), 'utf8'))
const notices = ['Third-party license notices\nGenerated from package-lock.json and installed packages.\n']
for (const relative of bundled) {
  const entry = lock.packages[relative]
  if (!entry) throw Error(`Missing locked package ${relative}`)
  const directory = resolve(root, relative)
  if (!directory.startsWith(root + '/node_modules/') && !directory.startsWith(root + '\\node_modules\\')) throw Error('Invalid package path')
  const files = readdirSync(directory).filter(name => /^(licen[cs]e|copying|notice)(\.|$)/i.test(name))
  notices.push(`\n${relative} @ ${entry.version}\nLicense: ${entry.license || 'See package source'}\n`)
  if (!files.length) {
    if (relative === 'node_modules/@vicons/ionicons5' && entry.version === '0.13.0') {
      notices.push(readFileSync(join(root, 'tools/licenses/ionicons5.txt'), 'utf8'))
    } else throw Error(`Missing license text for ${relative}; review before distributing build`)
  }
  for (const file of files) notices.push(readFileSync(join(directory, file), 'utf8'))
}
if (!existsSync(join(root, 'dist'))) throw Error('Build frontend first')
writeFileSync(join(root, 'dist/THIRD_PARTY_LICENSES.txt'), notices.join('\n'))
