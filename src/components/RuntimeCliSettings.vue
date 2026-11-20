<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMessage } from 'naive-ui'
import { invoke } from '@tauri-apps/api/core'

const props = defineProps<{ launcher: string }>()
const { t } = useI18n()
const message = useMessage()
type Action = 'status' | 'enable' | 'disable'
interface Registration {
  status: 'available' | 'enabled' | 'conflict' | 'moved'
  owned: boolean
  path: string | null
  changed: boolean
}
const registration = ref<Registration | null>(null)
const pending = ref<Action | null>(null)
const error = ref('')
const description = computed(() => {
  const state = registration.value
  if (!state) return ''
  switch (state.status) {
    case 'available': return t('settings.cliPathAvailable')
    case 'enabled': return t(state.owned ? 'settings.cliPathEnabled' : 'settings.cliPathExternal')
    case 'conflict': return t('settings.cliPathConflict')
    case 'moved': return t('settings.cliPathMoved')
  }
})

async function update(action: Action) {
  if (pending.value) return
  pending.value = action
  error.value = ''
  try {
    const state = await invoke<Registration>('manage_cli_path', { action })
    registration.value = state
    if (state.changed) message.success(t('settings.cliPathChanged'))
  } catch (reason) {
    registration.value = null
    error.value = String(reason)
  } finally {
    pending.value = null
  }
}

async function copyCommand() {
  try {
    await navigator.clipboard.writeText("& '" + props.launcher.replaceAll("'", "''") + "' --help")
    message.success(t('common.copied'))
  } catch {
    message.error(t('common.copyFailed'))
  }
}
onMounted(() => { void update('status') })
</script>

<template>
  <div class="runtime-cli">
    <div class="runtime-cli__launcher">
      <span>{{ t('settings.cliLauncherPath') }}</span>
      <code>{{ launcher }}</code>
    </div>
    <div class="runtime-cli__footer">
      <div class="runtime-cli__status" role="status" :data-status="error ? 'error' : registration?.status">
        <span v-if="error">{{ t('settings.cliPathFailed') }} {{ error }}</span>
        <span v-else-if="pending === 'status'">{{ t('settings.cliPathChecking') }}</span>
        <template v-else>
          <span>{{ description }}</span>
          <code v-if="registration?.path">{{ registration.path }}</code>
        </template>
      </div>
      <div class="runtime-cli__actions">
        <n-button v-if="registration?.status === 'available' || registration?.status === 'moved'"
          size="small" secondary type="primary" :loading="pending === 'enable'" :disabled="!!pending" @click="update('enable')">
          {{ t(registration.status === 'moved' ? 'settings.cliPathUpdate' : 'settings.cliPathAdd') }}
        </n-button>
        <n-button v-if="registration?.owned" size="small" tertiary
          :loading="pending === 'disable'" :disabled="!!pending" @click="update('disable')">
          {{ t('settings.cliPathRemove') }}
        </n-button>
        <n-button v-if="error || registration?.status === 'conflict'" size="small" tertiary
          :disabled="!!pending" @click="update('status')">
          {{ t('common.refresh') }}
        </n-button>
        <n-button size="small" secondary @click="copyCommand">{{ t('settings.copyCliCommand') }}</n-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.runtime-cli {
  display: grid;
  gap: 12px;
  min-width: 0;
}

.runtime-cli__launcher {
  display: grid;
  gap: 4px;
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid color-mix(in srgb, var(--outline) 42%, transparent);
  background: color-mix(in srgb, var(--surface-2) 52%, var(--surface-1));
  color: var(--on-surface-muted);
  font-size: 12px;
}

.runtime-cli code {
  color: var(--on-surface);
  font: 12px/1.55 "JetBrains Mono", "Cascadia Code", Consolas, "MiSans", ui-monospace, monospace;
  overflow-wrap: anywhere;
  user-select: text;
}

.runtime-cli__footer,
.runtime-cli__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
}

.runtime-cli__footer { gap: 10px 18px; }
.runtime-cli__actions { gap: 8px; }

.runtime-cli__status {
  flex: 1 1 18em;
  min-width: 0;
  color: var(--on-surface-muted);
  font-size: 12px;
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.runtime-cli__status code { display: block; margin-top: 2px; }
.runtime-cli__status[data-status="conflict"] { color: var(--warning); }
.runtime-cli__status[data-status="error"] { color: var(--danger); }
</style>
