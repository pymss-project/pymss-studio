import { createApp } from 'vue'
import { createI18n } from 'vue-i18n'
import App from './App.vue'
import { connect } from './sdk'
import { observeToolLayout } from './layout'
import { messages, resolveLocale } from './i18n'
import './features/audio-tools/styles.css'
import './style.css'

async function start() {
  const info = await connect()
  const i18n = createI18n({ legacy: false, locale: resolveLocale(info.locale), fallbackLocale: 'en', messages })
  const app = createApp(App)
  app.use(i18n)
  app.mount('#app')
  document.documentElement.lang = i18n.global.locale.value
  const stopLayout = observeToolLayout()
  window.addEventListener('pagehide', () => { stopLayout(); app.unmount() }, { once: true })
}
void start().catch(error => { document.getElementById('app')!.textContent = error instanceof Error ? error.message : String(error) })
