import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig, loadEnv, type Plugin } from 'vite'

function bundledLicenseInventory(): Plugin {
  return {
    name: 'bundled-license-inventory',
    generateBundle(_options, bundle) {
      const packages = new Set<string>()
      for (const output of Object.values(bundle)) {
        if (output.type !== 'chunk') continue
        for (const id of Object.keys(output.modules)) {
          const match = id.replaceAll('\\', '/').match(/\/node_modules\/((?:@[^/]+\/)?[^/]+)/)
          if (match) packages.add('node_modules/' + match[1])
        }
      }
      this.emitFile({ type: 'asset', fileName: 'third-party-packages.json', source: JSON.stringify([...packages].sort(), null, 2) })
    },
  }
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    plugins: [vue(), bundledLicenseInventory()],
    build: { manifest: true },
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
        'naive-ui': fileURLToPath(new URL('./src/compat/naive-ui.ts', import.meta.url)),
      },
    },
    server: {
      port: 5173,
      proxy: {
        '/api': env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:5500',
      },
    },
  }
})
