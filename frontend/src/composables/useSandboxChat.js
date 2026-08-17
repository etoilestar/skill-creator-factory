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
export function normalizeMultiSkillStepForUI(step = {}, index = 0) {
  const rawSources = step.input_sources ?? step.input_bindings ?? step.bindings ?? {}
  const inputSources = []
  if (Array.isArray(rawSources)) {
    for (const item of rawSources) {
      if (typeof item === 'string') inputSources.push(item)
      else if (item && typeof item === 'object') {
        inputSources.push(`${item.target || '?'} ← ${item.source || '?'}`)
      }
    }
  } else if (rawSources && typeof rawSources === 'object') {
    for (const [target, source] of Object.entries(rawSources)) {
      if (typeof source === 'string') inputSources.push(`${target} ← ${source}`)
      else if (source && typeof source === 'object') {
        if (source.source_type === 'skill_result') {
          const base = `${source.step_id || '?'}.${source.channel || '?'}`
          inputSources.push(`${target} ← ${appendJsonPath(base, source.path || '')}`)
        } else {
          inputSources.push(`${target} ← ${source.source_type || source.source || '?'}`)
        }
      }
    }
  }
  return {
    stepId: step.step_id || `step_${index + 1}`,
    skillName: step.skill_name || '',
    task: step.task || '',
    dependsOn: Array.isArray(step.depends_on) ? [...step.depends_on] : [],
    inputSources,
    childRunId: null,
    status: 'pending',
  }
}

export function appendJsonPath(base, path) {
  if (!path) return base
  return path.startsWith('[') ? `${base}${path}` : `${base}.${path}`
}

export function mapSandboxError(value) {
  const message = String(value || '')
  const known = {
    no_skill: '没有发现可执行的 Skill，请调整需求或检查技能是否已启用。',
    skill_not_executable: '选中的 Skill 当前不可执行，请检查技能状态。',
    multiskill_plan_not_found: '多技能方案不存在，请重新生成方案。',
    multiskill_plan_expired: '多技能方案已过期，请重新生成方案。',
  }
  const code = Object.keys(known).find(key => message.includes(key))
  return code ? known[code] : message.split('\n')[0]
}

export function applyMultiSkillEvent(state, event) {
  const data = event.data || {}
  if (event.type === 'multiskill_plan') {
    state.plan = data
    const displaySteps = data.preview?.length ? data.preview : data.steps || []
    state.steps = displaySteps.map(normalizeMultiSkillStepForUI)
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
    step = { stepId: data.step_id, skillName: data.skill_name || '', task: '', dependsOn: [], inputSources: [], childRunId: null, status: 'pending' }
    state.steps.push(step)
  }
  step.status = status
  if (data.child_run_id) step.childRunId = data.child_run_id
}

export function canUseSandboxMode(mode, skillName) {
  return mode === 'skill_pool' || Boolean(skillName)
}

export function sandboxChatUrl(mode, skillName = '') {
  return mode === 'skill_pool' ? '/api/chat/sandbox' : `/api/chat/sandbox/${encodeURIComponent(skillName)}`
}

export function sandboxUploadUrl(mode, skillName = '') {
  return mode === 'skill_pool' ? '/api/chat/sandbox/inputs' : `${sandboxChatUrl(mode, skillName)}/inputs`
}

export function buildSandboxRequestBody({ messages = [], model = null, executionMode = 'execute',
  sessionId, inputFiles = [], fields = {}, options = {}, resources = [], multiskillPlanId } = {}) {
  return {
    messages, model, execution_mode: executionMode, sandbox_session_id: sessionId,
    input_files: inputFiles, fields, options, resources,
    ...(multiskillPlanId ? { multiskill_plan_id: multiskillPlanId } : {}),
  }
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
