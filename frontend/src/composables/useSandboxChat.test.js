import test from 'node:test'
import assert from 'node:assert/strict'
import { applyMultiSkillEvent, parseSandboxSSEPayload } from './useSandboxChat.js'

test('parses every Multi-Skill SSE envelope without changing its data', () => {
  for (const type of ['multiskill_plan', 'skill_started', 'child_runtime_event', 'skill_completed', 'skill_failed', 'skill_ask_user', 'multi_skill_trace', 'multiskill_result']) {
    const data = { marker: type }
    assert.deepEqual(parseSandboxSSEPayload({ [type]: data }), { type, data })
  }
  assert.equal(parseSandboxSSEPayload({ answer: 'done' }), 'done')
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
