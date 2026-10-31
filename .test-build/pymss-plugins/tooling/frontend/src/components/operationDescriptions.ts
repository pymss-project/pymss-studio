/** Cache descriptions for the lifetime of one mounted tool, including in-flight requests. */
export function operationDescriptions<T>(load: (operation: string) => Promise<T>) {
  const resolved = new Map<string, T>()
  const pending = new Map<string, Promise<T>>()
  const generations = new Map<string, number>()
  return {
    peek: (operation: string) => resolved.get(operation),
    invalidate(operation: string) {
      resolved.delete(operation)
      pending.delete(operation)
      generations.set(operation, (generations.get(operation) || 0) + 1)
    },
    get(operation: string): Promise<T> {
      if (resolved.has(operation)) return Promise.resolve(resolved.get(operation)!)
      const existing = pending.get(operation)
      if (existing) return existing
      const generation = generations.get(operation) || 0
      const request = Promise.resolve().then(() => load(operation)).then(value => {
        if ((generations.get(operation) || 0) === generation) resolved.set(operation, value)
        return value
      }).finally(() => { if (pending.get(operation) === request) pending.delete(operation) })
      pending.set(operation, request)
      return request
    },
  }
}
