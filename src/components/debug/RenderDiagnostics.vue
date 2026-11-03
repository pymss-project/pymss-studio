<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { RendererDiagnosticSnapshot } from '@/features/diagnostics/businessDiagnostics'
defineProps<{ snapshot: RendererDiagnosticSnapshot | null; loading: boolean; error: string; disabled: boolean }>()
const emit = defineEmits<{ refresh: [] }>()
const { t } = useI18n()
</script>

<template>
  <n-card class="render-diagnostics" size="small" :title="t('debugSystem.renderTitle')">
    <template #header-extra><n-button size="small" secondary :loading="loading" :disabled="loading || disabled" @click="emit('refresh')">{{ t('common.refresh') }}</n-button></template>
    <p>{{ t('debugSystem.renderHint') }}</p>
    <n-alert v-if="error" type="error">{{ error }}</n-alert>
    <template v-if="snapshot">
      <p>WebView: {{ snapshot.webviewVersion || t('debug.stateUnknown') }} · {{ snapshot.os }} / {{ snapshot.arch }}</p>
      <n-alert v-if="snapshot.webviewError" type="warning">{{ snapshot.webviewError }}</n-alert>
      <n-alert v-if="snapshot.windows.missing.length" type="warning">{{ t('debugBusiness.missingWindows') }} {{ snapshot.windows.missing.join(', ') }}</n-alert>
      <section v-for="window in snapshot.windows.observed" :key="window.windowLabel" class="render-diagnostics-window">
        <strong>{{ window.windowLabel }}</strong><p>{{ window.observedAt }}</p>
        <p v-if="window.error" class="render-diagnostics-error">{{ window.error }}</p>
        <template v-if="window.rendering">
          <n-alert v-if="window.rendering.themeClass === 'mixed' || window.rendering.themeClass === 'missing'" type="warning">{{ t('debugSystem.themeClassWarning') }}</n-alert>
          <dl>
            <div><dt>{{ t('debugSystem.route') }}</dt><dd>{{ window.rendering.routePath }}</dd></div>
            <div><dt>{{ t('debugSystem.theme') }}</dt><dd>{{ window.rendering.configuredTheme }} / {{ window.rendering.resolvedTheme || '—' }} → {{ window.rendering.themeClass }}</dd></div>
            <div><dt>{{ t('debugSystem.animations') }}</dt><dd>{{ window.rendering.configuredAnimations }} / {{ window.rendering.animationsDisabled ?? t('debug.stateUnknown') }}</dd></div>
            <div><dt>{{ t('debugSystem.scale') }}</dt><dd>{{ window.rendering.configuredScale }} / {{ window.rendering.devicePixelRatio }}</dd></div>
            <div><dt>{{ t('debugSystem.viewport') }}</dt><dd>{{ window.rendering.viewportWidth }} × {{ window.rendering.viewportHeight }}</dd></div>
            <div><dt>{{ t('debugSystem.locale') }}</dt><dd>{{ window.rendering.configuredLocale }} → {{ window.rendering.resolvedLocale }}</dd></div>
            <div><dt>{{ t('debugSystem.colors') }}</dt><dd>{{ window.rendering.surfaceColor }} / {{ window.rendering.textColor }}</dd></div>
            <div><dt>{{ t('debugSystem.pageCounts') }}</dt><dd>{{ window.rendering.visiblePageRoots }} / {{ window.rendering.pageRoots }}</dd></div>
            <div><dt>{{ t('debugSystem.modalCounts') }}</dt><dd>{{ window.rendering.visibleModalRoots }} / {{ window.rendering.modalRoots }}</dd></div>
            <div><dt>User-Agent</dt><dd><code>{{ window.rendering.userAgent }}</code></dd></div>
          </dl>
        </template>
        <details v-if="window.changes?.length"><summary>{{ t('debugSystem.recentChanges') }}</summary>
          <p v-for="(change, index) in window.changes" :key="index">{{ change.at }} · {{ change.routePath }} · {{ change.theme }} · {{ change.animations }} · {{ change.scale }} · {{ change.locale }}</p>
        </details>
      </section>
    </template>
  </n-card>
</template>

<style scoped>
.render-diagnostics { margin-bottom: 16px; }
.render-diagnostics-window { border-top: 1px solid var(--border); padding: 10px 0; }
.render-diagnostics dl > div { display: grid; grid-template-columns: minmax(120px, 170px) minmax(0, 1fr); gap: 12px; padding: 5px 0; }
.render-diagnostics dt { color: var(--on-surface-muted); }
.render-diagnostics dd { margin: 0; }
.render-diagnostics code, .render-diagnostics p { overflow-wrap: anywhere; }
.render-diagnostics-error { color: var(--danger); }
@media (max-width: 600px) { .render-diagnostics dl > div { grid-template-columns: 1fr; gap: 4px; } }
</style>
