/** Returns true when GET /health responds 200 with {"status": "ok"}. */
export async function fetchHealth(signal?: AbortSignal): Promise<boolean> {
  const response = await fetch('/health', { signal })
  if (!response.ok) return false
  const body: unknown = await response.json()
  return typeof body === 'object' && body !== null && (body as { status?: unknown }).status === 'ok'
}
