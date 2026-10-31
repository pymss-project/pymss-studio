type Guard = () => void | Promise<void>
const guards = new Set<Guard>()
export function registerWindowCloseGuard(guard: Guard, _priority = 0) {
  guards.add(guard)
  return () => guards.delete(guard)
}
export async function releaseTool() {
  for (const guard of guards) await guard()
}
