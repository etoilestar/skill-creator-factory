import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const creatorSource = readFileSync(resolve(__dirname, '../src/views/CreatorView.vue'), 'utf8')
const thinkingSource = readFileSync(resolve(__dirname, '../src/components/ThinkingPanel.vue'), 'utf8')

function createHarness() {
  let pendingFunctionItems = [{ target_file: 'scripts/a.py', purpose: 'A' }]
  let pendingResponsibilityEdges = [{ from_node: 'platform_input_node', to_node: 'scripts/a.py' }]
  let resolvedFunctionItems = null
  let resolvedResponsibilityEdges = null
  const thoughts = []

  const saveResolvedGraphSnapshot = ({ functionItems, responsibilityEdges } = {}) => {
    if (Array.isArray(functionItems)) resolvedFunctionItems = functionItems
    if (Array.isArray(responsibilityEdges)) resolvedResponsibilityEdges = responsibilityEdges
  }
  const onPlanner = (event) => {
    if (Array.isArray(event.function_items)) pendingFunctionItems = event.function_items
    if (Array.isArray(event.responsibility_edges)) pendingResponsibilityEdges = event.responsibility_edges
    if (event.event === 'planner_converged' || event.event === 'graph_resolved') {
      saveResolvedGraphSnapshot({ functionItems: event.function_items, responsibilityEdges: event.responsibility_edges })
    }
  }
  const onPlan = (plan) => {
    if (Array.isArray(plan.responsibility_edges)) pendingResponsibilityEdges = plan.responsibility_edges
    if (Array.isArray(plan.function_items)) pendingFunctionItems = plan.function_items
    saveResolvedGraphSnapshot({ functionItems: plan.function_items, responsibilityEdges: plan.responsibility_edges })
  }
  const appendExecutionBlock = ({ step, label, detail = '', content = '' } = {}) => {
    thoughts.push({ step: String(step || 'execution'), label: String(label || '执行步骤'), detail: String(detail || ''), content })
  }
  const clearChat = () => {
    pendingFunctionItems = []
    pendingResponsibilityEdges = []
    resolvedFunctionItems = null
    resolvedResponsibilityEdges = null
    thoughts.length = 0
  }
  return {
    get pendingFunctionItems() { return pendingFunctionItems },
    get pendingResponsibilityEdges() { return pendingResponsibilityEdges },
    get resolvedFunctionItems() { return resolvedFunctionItems },
    get resolvedResponsibilityEdges() { return resolvedResponsibilityEdges },
    thoughts,
    onPlanner,
    onPlan,
    appendExecutionBlock,
    clearChat,
  }
}

describe('CreatorView graph persistence and execution process', () => {
  it('does not clear existing graph when planner event omits graph arrays', () => {
    const h = createHarness()
    h.onPlanner({ event: 'planner_draft' })
    assert.equal(h.pendingFunctionItems.length, 1)
    assert.equal(h.pendingResponsibilityEdges.length, 1)
  })

  it('preserves explicit empty arrays as a valid resolved empty graph', () => {
    const h = createHarness()
    h.onPlanner({ event: 'planner_converged', function_items: [], responsibility_edges: [] })
    assert.deepEqual(h.pendingFunctionItems, [])
    assert.deepEqual(h.pendingResponsibilityEdges, [])
    assert.deepEqual(h.resolvedFunctionItems, [])
    assert.deepEqual(h.resolvedResponsibilityEdges, [])
  })

  it('keeps resolved graph through file generation and creation-complete events', () => {
    const h = createHarness()
    h.onPlan({ function_items: [{ target_file: 'scripts/a.py' }], responsibility_edges: [{ from_node: 'scripts/a.py' }] })
    h.appendExecutionBlock({ step: 'file_generation_start', label: '文件开始生成' })
    h.appendExecutionBlock({ step: 'creation_complete', label: '创建完成' })
    assert.equal(h.resolvedFunctionItems.length, 1)
    assert.equal(h.resolvedResponsibilityEdges.length, 1)
  })

  it('only clearChat clears graph state', () => {
    const h = createHarness()
    h.onPlan({ function_items: [{ target_file: 'scripts/a.py' }], responsibility_edges: [{ from_node: 'scripts/a.py' }] })
    h.clearChat()
    assert.deepEqual(h.pendingFunctionItems, [])
    assert.deepEqual(h.pendingResponsibilityEdges, [])
    assert.equal(h.resolvedFunctionItems, null)
    assert.equal(h.resolvedResponsibilityEdges, null)
  })

  it('starts execution process with the user input block', () => {
    const h = createHarness()
    h.appendExecutionBlock({ step: 'user_input', label: '用户输入', content: '创建一个 Skill' })
    assert.equal(h.thoughts[0].step, 'user_input')
  })

  it('ThinkingPanel no longer stringifies thought.data', () => {
    assert.equal(thinkingSource.includes('JSON.stringify(thought.data'), false)
    assert.match(thinkingSource, /thought\.content/)
  })

  it('execution blocks do not persist raw payloads, full plans, or full graph object fields', () => {
    const start = creatorSource.indexOf('function appendExecutionBlock')
    const end = creatorSource.indexOf('function summarizeFiles', start)
    const body = creatorSource.slice(start, end)
    for (const forbidden of ['raw', 'payload', 'plan:', 'function_items', 'responsibility_edges', 'tool_pool_summary']) {
      assert.equal(body.includes(forbidden), false, `${forbidden} must not be stored by appendExecutionBlock`)
    }
  })
})
