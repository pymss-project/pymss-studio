/** Shared types for pymss-studio web mode (browser <-> Node server). */

export interface WebAppPaths {
  dataRoot: string
  settingsDir: string
  modelsDir: string
  outputsDir: string
  editorProjectsDir: string
  logsDir: string
  tempDir: string
}

export interface WebSessionInfo {
  sessionId: string
  /** null when no authentication is configured */
  authRequired: boolean
  version?: string
}
