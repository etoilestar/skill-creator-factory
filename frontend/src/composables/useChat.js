/**
 * Stream an SSE chat response from the backend.
 *
 * Yields either:
 *   - a plain string  — text content chunk
 *   - { type: 'action_result', data: {...} } — skill file-operation result
 *   - { type: 'status', data: {phase, message} | null } — execution phase update
 *   - { type: 'thought', data: {step, label, detail, data, ts} } — internal decision record
 *   - { type: 'quick_actions', data: {actions: [...], ts} } — quick action buttons to display
 *   - { type: 'plan_preview', data: {...} } — plan preview awaiting confirmation (Plan mode)
 *   - { type: 'sop_plan', data: {...} } — SOP document generated from the plan
 *   - { type: 'task_checklist', data: {tasks, completed_indices, executing_index, ts} } — inline task checklist
 *   - { type: 'sandbox_retry', data: {attempt, max_retries, error, corrected, ts} } — sandbox retry notification
 *
 * @param {string} url  - POST endpoint (e.g. /api/chat/creator)
 * @param {object} body - { messages: [{role, content}], model?, execution_mode? }
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
        else if (parsed.thought) yield { type: 'thought', data: parsed.thought }
        else if ('status' in parsed) yield { type: 'status', data: parsed.status }
        else if (parsed.quick_actions) yield { type: 'quick_actions', data: parsed.quick_actions }
        else if (parsed.plan_preview) yield { type: 'plan_preview', data: parsed.plan_preview }
        else if (parsed.sop_plan) yield { type: 'sop_plan', data: parsed.sop_plan }
        else if (parsed.task_progress) yield { type: 'task_progress', data: parsed.task_progress }
        else if (parsed.task_checklist) yield { type: 'task_checklist', data: parsed.task_checklist }
        else if (parsed.sandbox_retry) yield { type: 'sandbox_retry', data: parsed.sandbox_retry }
        else if (parsed.content) yield parsed.content
      } catch (e) {
        // skip unparseable lines
        if (e.message && !e.message.startsWith('JSON')) throw e
      }
    }
  }
}

/**
 * Confirm or cancel a pending plan execution.
 *
 * @param {string} skillName - The skill name
 * @param {string} planId - The plan ID to confirm
 * @param {string} action - "confirm" or "cancel"
 * @returns {Promise<Response>} - streaming response for confirm, JSON for cancel
 */
export async function confirmPlan(skillName, planId, action = 'confirm') {
  const url = `/api/chat/sandbox/${encodeURIComponent(skillName)}/confirm`
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plan_id: planId, action }),
  })

  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(err.detail || 'Confirm request failed')
  }

  return response
}

/**
 * Stream the confirmed plan execution result.
 * Re-uses the SSE parsing logic from streamChat.
 *
 * @param {Response} response - The fetch response from confirmPlan
 * @yields {string | {type: string, data: object}}
 */
export async function* streamConfirmResponse(response) {
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop()

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      const data = line.slice(6).trim()
      if (data === '[DONE]') return
      try {
        const parsed = JSON.parse(data)
        if (parsed.error) throw new Error(parsed.error)
        if (parsed.action_result) yield { type: 'action_result', data: parsed.action_result }
        else if (parsed.thought) yield { type: 'thought', data: parsed.thought }
        else if ('status' in parsed) yield { type: 'status', data: parsed.status }
        else if (parsed.quick_actions) yield { type: 'quick_actions', data: parsed.quick_actions }
        else if (parsed.plan_preview) yield { type: 'plan_preview', data: parsed.plan_preview }
        else if (parsed.sop_plan) yield { type: 'sop_plan', data: parsed.sop_plan }
        else if (parsed.task_progress) yield { type: 'task_progress', data: parsed.task_progress }
        else if (parsed.task_checklist) yield { type: 'task_checklist', data: parsed.task_checklist }
        else if (parsed.sandbox_retry) yield { type: 'sandbox_retry', data: parsed.sandbox_retry }
        else if (parsed.content) yield parsed.content
      } catch (e) {
        if (e.message && !e.message.startsWith('JSON')) throw e
      }
    }
  }
}
