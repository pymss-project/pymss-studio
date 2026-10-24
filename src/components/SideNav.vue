<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useSettingsStore } from '@/stores/settings'
import {
  MusicalNotesOutline,
  CubeOutline,
  GitNetworkOutline,
  FolderOpenOutline,
  ConstructOutline,
  ExtensionPuzzleOutline,
  SettingsOutline,
  TerminalOutline,
} from '@vicons/ionicons5'

const route = useRoute()
const { t } = useI18n()
const settings = useSettingsStore()
const mainItems = computed(() => [
  { name: 'separate', path: '/', icon: MusicalNotesOutline, label: t('nav.separate') },
  { name: 'models', path: '/models', icon: CubeOutline, label: t('nav.models') },
  { name: 'workflows', path: '/workflows', icon: GitNetworkOutline, label: t('nav.workflows') },
  { name: 'results', path: '/results', icon: FolderOpenOutline, label: t('nav.results') },
  { name: 'tools', path: '/tools', icon: ConstructOutline, label: t('nav.tools') },
  { name: 'plugins', path: '/plugins', icon: ExtensionPuzzleOutline, label: t('nav.plugins') },
])
const bottomItems = computed(() => [
  ...(settings.developerMode ? [{ name: 'debug', path: '/debug', icon: TerminalOutline, label: t('nav.debug') }] : []),
  { name: 'settings', path: '/settings', icon: SettingsOutline, label: t('nav.settings') },
])
</script>

<template>
  <aside class="side-nav" data-guide="workspace-navigation">
    <div class="side-nav__main">
      <router-link
        v-for="item in mainItems"
        :key="item.name"
        class="nav-item"
        :class="{ active: route.name === item.name }"
        :to="item.path"
        :title="item.label"
      >
        <span class="nav-icon">
          <n-icon :component="item.icon" :size="18" />
        </span>
        <span>{{ item.label }}</span>
      </router-link>
    </div>
    <div class="side-nav__bottom">
      <router-link
        v-for="item in bottomItems"
        :key="item.name"
        class="nav-item"
        :class="[
          { active: route.name === item.name },
          'nav-item--utility',
          item.name === 'debug' ? 'nav-item--debug' : '',
        ]"
        :to="item.path"
        :title="item.label"
      >
        <span class="nav-icon">
          <n-icon :component="item.icon" :size="18" />
        </span>
        <span>{{ item.label }}</span>
      </router-link>
    </div>
  </aside>
</template>
