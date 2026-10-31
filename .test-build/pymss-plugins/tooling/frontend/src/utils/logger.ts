export function createLogger(_name: string) {
  return {
    debug: (..._values: unknown[]) => undefined,
    info: (..._values: unknown[]) => undefined,
    warn: (message: string, ..._context: unknown[]) => console.warn(message),
    error: (message: string, ..._context: unknown[]) => console.error(message),
  }
}
