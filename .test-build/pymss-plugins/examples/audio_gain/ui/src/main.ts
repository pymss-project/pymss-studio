import { createApp } from 'vue'
import { createI18n } from 'vue-i18n'
import App from './App.vue'
import { host, LAYOUT_REFRESH_EVENT } from './studio'
import { messages } from './i18n'
import './style.css'

const i18n = createI18n({ legacy: false, locale: host.locale, fallbackLocale: 'en', messages })
const app = createApp(App).use(i18n)
app.mount('#app')

let scheduled = 0
const reportHeight = () => {
  cancelAnimationFrame(scheduled)
  scheduled = requestAnimationFrame(() => {
    const height = Math.ceil(document.getElementById('app')!.getBoundingClientRect().height)
    window.parent.postMessage({ jsonrpc: '2.0', method: 'pymss.resize', params: { height } }, '*')
  })
}
const observer = new ResizeObserver(reportHeight)
observer.observe(document.getElementById('app')!)
window.addEventListener(LAYOUT_REFRESH_EVENT, reportHeight)
reportHeight()
window.addEventListener('pagehide', () => {
  observer.disconnect()
  window.removeEventListener(LAYOUT_REFRESH_EVENT, reportHeight)
  cancelAnimationFrame(scheduled)
  app.unmount()
}, { once: true })
