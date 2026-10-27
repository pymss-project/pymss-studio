import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'node:path'
import Components from 'unplugin-vue-components/vite'
import { NaiveUiResolver } from 'unplugin-vue-components/resolvers'

export default defineConfig({
  plugins: [
    {
      name: 'studio-native-boundary',
      enforce: 'pre',
      resolveId(source, importer) {
        if (source === '@tauri-core-native') return resolve(__dirname, 'node_modules/@tauri-apps/api/core.js')
        // SDK modules also import core relatively; both paths must honor the tutorial's private transport.
        if (source === '@tauri-apps/api/core'
          || (source === './core.js' && importer?.replace(/\\/g, '/').includes('/@tauri-apps/api/'))) {
          return resolve(__dirname, 'src/utils/tauriCore.ts')
        }
      },
    },
    vue(),
    Components({
      resolvers: [NaiveUiResolver()],
      dts: false,
      types: [],
    }),
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  clearScreen: false,
  optimizeDeps: {
    exclude: ['@tauri-apps/api', '@tauri-apps/plugin-dialog', '@tauri-apps/plugin-shell', '@tauri-apps/plugin-process', '@tauri-apps/plugin-updater', '@tauri-apps/plugin-store'],
  },
  server: {
    port: 1420,
    strictPort: true,
    watch: { ignored: ['**/src-tauri/**'] },
    // Web-mode development: `pnpm dev` + `node scripts/web-server.mjs` (see docs/web-mode.md).
    proxy: {
      '/api': {
        target: process.env.PYMSS_WEB_DEV_SERVER || 'http://127.0.0.1:8710',
        changeOrigin: true,
      },
      '/login': {
        target: process.env.PYMSS_WEB_DEV_SERVER || 'http://127.0.0.1:8710',
        changeOrigin: true,
      },
    },
  },
})
