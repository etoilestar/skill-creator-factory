/** Sandbox-only SSE transport. Shared chat parsing remains untouched. */

export function parseSandboxSSEPayload(payload) {
  if (payload.error) throw new Error(payload.error)
  if (payload.type === 'error') throw new Error(payload.message || '执行失败')
  if (payload.result_manifest) return { type: 'result_manifest', data: payload.result_manifest }
  const multiSkillEvents = [
    'multiskill_plan', 'skill_started', 'child_runtime_event', 'skill_completed',
    'skill_failed', 'skill_ask_user', 'multi_skill_trace', 'multiskill_result',
  ]
  for (const type of multiSkillEvents) {
    if (payload[type]) return { type, data: payload[type] }
  }
  if (payload.action_result) return { type: 'action_result', data: payload.action_result }
  if (payload.thought) return { type: 'thought', data: payload.thought }
  if ('status' in payload) return { type: 'status', data: payload.status }
  if (payload.plan_preview) return { type: 'plan_preview', data: payload.plan_preview }
  if (payload.sop_plan) return { type: 'sop_plan', data: payload.sop_plan }
  if (payload.task_progress) return { type: 'task_progress', data: payload.task_progress }
  if (payload.task_checklist) return { type: 'task_checklist', data: payload.task_checklist }
  if (payload.sandbox_retry) return { type: 'sandbox_retry', data: payload.sandbox_retry }
  if (payload.type === 'step_skipped') return { type: 'step_skipped', data: payload.data }
  if (payload.model_ack) return { type: 'model_ack', data: payload.model_ack }
  if (payload.answer) return payload.answer
  if (payload.content) return payload.content
  return null
}

/** Keep parent Skill state separate from each Child Runtime's private event list. */
export function applyMultiSkillEvent(state, event) {
  const data = event.data || {}
  if (event.type === 'multiskill_plan') {
    state.plan = data
    state.steps = (data.steps || data.preview || []).map((step, index) => ({
      stepId: step.step_id || `step_${index + 1}`,
      skillName: step.skill_name || '',
      task: step.task || '',
      dependsOn: step.depends_on || [],
      inputSources: step.input_sources || step.input_bindings || {},
      childRunId: null,
      status: 'pending',
    }))
    return
  }
  if (event.type === 'child_runtime_event') {
    if (!data.child_run_id) return
    ;(state.childEvents[data.child_run_id] ||= []).push(data.event)
    return
  }
  if (event.type === 'multi_skill_trace') {
    state.trace = Array.isArray(data) ? data : []
    return
  }
  if (event.type === 'multiskill_result') {
    state.result = data
    if (Array.isArray(data.multi_skill_trace)) state.trace = data.multi_skill_trace
    return
  }
  const statuses = {
    skill_started: 'running', skill_completed: 'completed',
    skill_failed: 'failed', skill_ask_user: 'ask_user',
  }
  const status = statuses[event.type]
  if (!status) return
  let step = state.steps.find(item => item.stepId === data.step_id)
  if (!step) {
    step = { stepId: data.step_id, skillName: data.skill_name || '', task: '', dependsOn: [], inputSources: {}, childRunId: null, status: 'pending' }
    state.steps.push(step)
  }
  step.status = status
  if (data.child_run_id) step.childRunId = data.child_run_id
}

export function clearSandboxRoundResult(resultManifest) {
  resultManifest.value = null
}

async function* parseSandboxStream(response) {
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
    const lines = buffer.split('\n')
    buffer = done ? '' : lines.pop()
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      const data = line.slice(6).trim()
      if (data === '[DONE]') return
      try {
        const event = parseSandboxSSEPayload(JSON.parse(data))
        if (event !== null) yield event
      } catch (error) {
        if (!(error instanceof SyntaxError)) throw error
      }
    }
    if (done) return
  }
}

export async function* streamChat(url, body) {
  const response = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(error.detail || 'Request failed')
  }
  yield* parseSandboxStream(response)
}

export async function confirmPlan(skillName, planId, action = 'confirm') {
  const response = await fetch(`/api/chat/sandbox/${encodeURIComponent(skillName)}/confirm`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ plan_id: planId, action }),
  })
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(error.detail || 'Confirm request failed')
  }
  return response
}

export async function* streamConfirmResponse(response) {
  yield* parseSandboxStream(response)
}
