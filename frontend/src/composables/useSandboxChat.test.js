import test from 'node:test'
import assert from 'node:assert/strict'
import { applyMultiSkillEvent, buildSandboxRequestBody, canUseSandboxMode,
  normalizeMultiSkillStepForUI, parseSandboxSSEPayload, sandboxChatUrl, sandboxUploadUrl } from './useSandboxChat.js'

test('parses every Multi-Skill SSE envelope without changing its data', () => {
  for (const type of ['multiskill_plan', 'skill_started', 'child_runtime_event', 'skill_completed', 'skill_failed', 'skill_ask_user', 'multi_skill_trace', 'multiskill_result']) {
    const data = { marker: type }
    assert.deepEqual(parseSandboxSSEPayload({ [type]: data }), { type, data })
  }
  assert.equal(parseSandboxSSEPayload({ answer: 'done' }), 'done')
})

test('normalizes preview input_sources object arrays for display', () => {
  const step = normalizeMultiSkillStepForUI({ step_id: 's2', input_sources: [
    { target: 'payload', source: 's1.structured_outputs[0].data.summary' },
  ] })
  assert.deepEqual(step.inputSources, ['payload ← s1.structured_outputs[0].data.summary'])
  assert.equal(step.inputSources.join('；').includes('[object Object]'), false)
})

test('normalizes canonical bindings without mutating protocol data', () => {
  const binding = { source_type: 'skill_result', step_id: 's1', channel: 'structured_outputs', path: '[0].data.summary' }
  const canonical = { step_id: 's2', bindings: { payload: binding }, depends_on: ['s1'] }
  const step = normalizeMultiSkillStepForUI(canonical)
  assert.deepEqual(step.inputSources, ['payload ← s1.structured_outputs[0].data.summary'])
  assert.deepEqual(canonical.bindings.payload, binding)
})

test('builds mode-specific URLs and allows Skill Pool without selectedSkill', () => {
  assert.equal(canUseSandboxMode('skill_pool', ''), true)
  assert.equal(canUseSandboxMode('single_skill', ''), false)
  assert.equal(sandboxChatUrl('skill_pool', ''), '/api/chat/sandbox')
  assert.equal(sandboxChatUrl('single_skill', 'document skill'), '/api/chat/sandbox/document%20skill')
  assert.equal(sandboxUploadUrl('skill_pool', ''), '/api/chat/sandbox/inputs')
  assert.equal(sandboxUploadUrl('single_skill', 'one'), '/api/chat/sandbox/one/inputs')
})

test('builds Skill Pool confirmation body without replaying user messages', () => {
  const body = buildSandboxRequestBody({ sessionId: 'session-1', multiskillPlanId: 'plan-1' })
  assert.equal(sandboxChatUrl('skill_pool'), '/api/chat/sandbox')
  assert.equal(body.multiskill_plan_id, 'plan-1')
  assert.deepEqual(body.messages, [])
})

test('updates parent steps and isolates Child Runtime events by child_run_id', () => {
  const state = { plan: null, trace: [], result: null, steps: [], childEvents: {} }
  applyMultiSkillEvent(state, { type: 'multiskill_plan', data: { steps: [
    { step_id: 'one', skill_name: 'document-analysis', task: '分析' },
    { step_id: 'two', skill_name: 'report-generator', task: '报告', depends_on: ['one'] },
  ] } })
  applyMultiSkillEvent(state, { type: 'skill_started', data: { step_id: 'one', child_run_id: 'child_1' } })
  applyMultiSkillEvent(state, { type: 'child_runtime_event', data: { child_run_id: 'child_1', event: { step: 'inner-a' } } })
  applyMultiSkillEvent(state, { type: 'child_runtime_event', data: { child_run_id: 'child_2', event: { step: 'inner-b' } } })
  assert.equal(state.steps[0].status, 'running')
  assert.deepEqual(state.childEvents.child_1, [{ step: 'inner-a' }])
  assert.deepEqual(state.childEvents.child_2, [{ step: 'inner-b' }])
  applyMultiSkillEvent(state, { type: 'skill_completed', data: { step_id: 'one' } })
  applyMultiSkillEvent(state, { type: 'skill_failed', data: { step_id: 'two', child_run_id: 'child_2' } })
  assert.deepEqual(state.steps.map(step => step.status), ['completed', 'failed'])
})
