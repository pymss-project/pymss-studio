export type WorkflowMetadata = {
  guid: string
  author?: string
  version?: string
  studioVersion?: string
}

const GUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const TEXT_LIMITS = { author: 200, version: 64, studioVersion: 128 } as const

export function normalizeWorkflowMetadata(value: unknown): WorkflowMetadata | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const source = value as Record<string, unknown>
  if (typeof source.guid !== 'string' || !GUID.test(source.guid.trim())) return undefined
  const metadata: WorkflowMetadata = { guid: source.guid.trim().toLowerCase() }
  for (const key of Object.keys(TEXT_LIMITS) as Array<keyof typeof TEXT_LIMITS>) {
    if (source[key] === undefined) continue
    const text = source[key]
    if (typeof text !== 'string' || text.length > TEXT_LIMITS[key] || /[\u0000-\u001f]/.test(text)) return undefined
    if (text.trim()) metadata[key] = text.trim()
  }
  return metadata
}

export function createWorkflowMetadata(value?: unknown, newIdentity = false): WorkflowMetadata {
  const metadata = normalizeWorkflowMetadata(value)
  return { ...metadata, guid: !newIdentity && metadata ? metadata.guid : createGuid() }
}

export function stripWorkflowMetadata(definition: Record<string, unknown>): Record<string, unknown> {
  const { _metadata, ...executionDefinition } = definition
  return executionDefinition
}

export function parseWorkflowFile(text: string): { definition: Record<string, unknown>; metadata?: WorkflowMetadata } {
  const parsed: unknown = JSON.parse(text)
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Invalid workflow definition')
  const source = parsed as Record<string, unknown>
  const metadata = normalizeWorkflowMetadata(source._metadata)
  if (Object.hasOwn(source, '_metadata') && !metadata) throw new Error('Invalid workflow metadata')
  return { definition: stripWorkflowMetadata(source), metadata }
}

export function serializeWorkflowFile(definition: Record<string, unknown>, metadata: WorkflowMetadata): string {
  const normalized = normalizeWorkflowMetadata(metadata)
  if (!normalized) throw new Error('Invalid workflow metadata')
  return `${JSON.stringify({ _metadata: normalized, ...stripWorkflowMetadata(definition) }, null, 2)}\n`
}

function createGuid(): string {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID()
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
