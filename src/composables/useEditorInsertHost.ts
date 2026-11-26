import { inject, provide, type InjectionKey, type Ref } from 'vue'
import type { PluginHostCapabilities, UnavailablePlugin } from '@/utils/pluginHost'

/**
 * Insert operations shared by every insert list in the editor. The plugin manager owns
 * the host session and state capture; lists only describe which channel they act on.
 * A null track id addresses the master bus.
 */
export interface EditorInsertHost {
  capabilities: Readonly<Ref<PluginHostCapabilities | null>>
  busy: Readonly<Ref<boolean>>
  opened: Readonly<Ref<string[]>>
  unavailablePlugins: Readonly<Ref<UnavailablePlugin[]>>
  operationError: Readonly<Ref<string>>
  dialogPending: Readonly<Ref<boolean>>
  managerOpen: Readonly<Ref<boolean>>
  openManager: (trackId: string | null) => void
  addInsert: (trackId: string | null) => void
  openParameters: (trackId: string | null, slotId: string) => void
  toggleEditor: (trackId: string | null, slotId: string) => Promise<void>
  setBypassed: (trackId: string | null, slotId: string, bypassed: boolean) => Promise<void>
  move: (trackId: string | null, slotId: string, offset: number) => Promise<void>
  remove: (trackId: string | null, slotId: string) => Promise<void>
  handleKeydown: (event: KeyboardEvent) => void
}

const EDITOR_INSERT_HOST: InjectionKey<Readonly<Ref<EditorInsertHost | null>>> = Symbol('editor-insert-host')

export function provideEditorInsertHost(host: Readonly<Ref<EditorInsertHost | null>>) {
  provide(EDITOR_INSERT_HOST, host)
}

export function useEditorInsertHost() {
  const host = inject(EDITOR_INSERT_HOST, null)
  if (!host) throw new Error('Editor insert host is not available.')
  return host
}
