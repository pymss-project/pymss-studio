<script setup lang="ts">
import { computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { darkTheme, zhCN, enUS } from 'naive-ui'
import ToolMount from './ToolMount.vue'
import { presentation } from './presentation'
import { persistenceError } from './sdk'
import { resolveLocale } from './i18n'

const theme = computed(() => presentation.mode === 'dark' ? darkTheme : null)
const { locale } = useI18n()
watch(() => presentation.locale, value => {
  locale.value = resolveLocale(value)
  document.documentElement.lang = locale.value
}, { immediate: true })
const componentLocale = computed(() => locale.value.startsWith('zh') ? zhCN : enUS)
const overrides = computed(() => ({
  ...(presentation.componentTheme || {}),
  common: { primaryColor: presentation.primary, primaryColorHover: presentation.primary, primaryColorPressed: presentation.primary, ...presentation.componentTheme?.common, fontFamily: presentation.fontFamily },
}))
</script>
<template>
  <n-config-provider :theme="theme" :theme-overrides="overrides" :locale="componentLocale">
    <n-message-provider><n-dialog-provider>
      <n-alert v-if="persistenceError" type="warning" :bordered="false">{{ persistenceError }}</n-alert>
      <ToolMount />
    </n-dialog-provider></n-message-provider>
  </n-config-provider>
</template>
