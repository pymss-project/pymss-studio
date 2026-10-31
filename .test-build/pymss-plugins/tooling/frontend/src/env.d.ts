/// <reference types="vite/client" />
declare const __FILE_TOOL__: string
declare const __OPERATION_ID__: string
declare const __USE_FILE_TOOL_RUNTIME__: boolean
declare const __STUDIO_REQUIREMENTS__: { sdkVersion: number; requiredCapabilities: string[] }
declare module '@tool/entry' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent
  export default component
}
declare module '@tool/locales' {
  import type { DefineLocaleMessage } from 'vue-i18n'
  const messages: Record<string, DefineLocaleMessage>
  export default messages
}
