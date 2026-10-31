export type OperationSchema = { required?: string[]; properties?: Record<string, Record<string, unknown>> }

export function schemaDefaults(schema: OperationSchema | null | undefined) {
  return Object.fromEntries(Object.entries(schema?.properties || {}).flatMap(([key, field]) => field.default !== undefined ? [[key, field.default]] : field.type === 'boolean' ? [[key, false]] : []))
}

export function supportsOperationSchema(schema: OperationSchema | null | undefined) {
  if (!schema?.properties || typeof schema.properties !== 'object' || Array.isArray(schema.properties)) return false
  return Object.values(schema.properties).every(field => field && typeof field === 'object'
    && ['number', 'integer', 'string', 'boolean'].includes(String(field.type))
    && (field.enum === undefined || Array.isArray(field.enum)))
}

export function validateParameters(schema: OperationSchema | null | undefined, values: Record<string, unknown>) {
  const errors: Record<string, string> = {}
  for (const [key, field] of Object.entries(schema?.properties || {})) {
    const value = values[key]
    if ((schema?.required || []).includes(key) && (value === undefined || value === null || value === '')) errors[key] = 'required'
    if (value === undefined || value === null) continue
    if (field.type === 'string' && typeof value !== 'string') errors[key] = 'string'
    if (field.type === 'boolean' && typeof value !== 'boolean') errors[key] = 'boolean'
    if (field.type === 'number' || field.type === 'integer') {
      if (typeof value !== 'number' || !Number.isFinite(value) || (field.type === 'integer' && !Number.isInteger(value))) errors[key] = 'number'
      else {
        if (typeof field.minimum === 'number' && value < field.minimum) errors[key] = 'minimum'
        if (typeof field.maximum === 'number' && value > field.maximum) errors[key] = 'maximum'
      }
    }
    if (Array.isArray(field.enum) && !field.enum.some(item => Object.is(item, value))) errors[key] = 'enum'
  }
  return errors
}
