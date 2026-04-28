/**
 * Stream an SSE chat response from the backend.
 *
 * Yields either:
 *   - a plain string  — text content chunk
 *   - { type: 'action_result', data: {...} } — skill file-operation result
 *
 * @param {string} url  - POST endpoint (e.g. /api/chat/creator)
 * @param {object} body - { messages: [{role, content}], model? }
 * @yields {string | {type: string, data: object}}
 */
export async function* streamChat(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(err.detail || 'Request failed')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() // keep possibly incomplete last line

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      const data = line.slice(6).trim()
      if (data === '[DONE]') return
      try {
        const parsed = JSON.parse(data)
        if (parsed.error) throw new Error(parsed.error)
        if (parsed.action_result) yield { type: 'action_result', data: parsed.action_result }
        else if (parsed.content) yield parsed.content
      } catch (e) {
        // skip unparseable lines
        if (e.message && !e.message.startsWith('JSON')) throw e
      }
    }
  }
}
