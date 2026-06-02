/**
 * Stream an SSE chat response from the backend.
 *
 * Yields either:
 *   - a plain string  — text content chunk
 *   - { type: 'action_result', data: {...} } — skill file-operation result
 *   - { type: 'status', data: {phase, message} | null } — execution phase update
 *   - { type: 'thought', data: {step, label, detail, data, ts} } — internal decision record
 *   - { type: 'quick_actions', data: {actions: [...], ts} } — quick action buttons to display
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
        if (parsed.type === 'error') throw new Error(parsed.message || '执行失败')
        if (parsed.action_result) yield { type: 'action_result', data: parsed.action_result }
        else if (parsed.thought) yield { type: 'thought', data: parsed.thought }
        else if ('status' in parsed) yield { type: 'status', data: parsed.status }
        else if (parsed.type === 'phase3_start') yield { type: 'status', data: { phase: 'phase3', message: parsed.message || '开始执行 Skill 创建流程…' } }
        else if (parsed.type === 'progress') yield { type: 'status', data: { phase: 'phase3', message: parsed.step || parsed.message || '正在执行…' } }
        else if (parsed.type === 'completed') yield {
          type: 'action_result',
          data: {
            action: 'creator_phase3_completed',
            success: parsed.success,
            name: parsed.skill_name,
            path: parsed.skill_path,
            message: parsed.message || 'Skill 创建完成',
            output_files: parsed.created_files || [],
          },
        }
        else if (parsed.quick_actions) yield { type: 'quick_actions', data: parsed.quick_actions }
        else if (parsed.content) yield parsed.content
      } catch (e) {
        // skip unparseable lines
        if (e.message && !e.message.startsWith('JSON')) throw e
      }
    }
  }
}
