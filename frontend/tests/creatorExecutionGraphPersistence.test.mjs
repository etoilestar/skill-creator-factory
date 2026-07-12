import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const creatorSource = readFileSync(resolve(__dirname, '../src/views/CreatorView.vue'), 'utf8')
const thinkingSource = readFileSync(resolve(__dirname, '../src/components/ThinkingPanel.vue'), 'utf8')
const creatorExecutionPanelSource = readFileSync(resolve(__dirname, '../src/components/CreatorExecutionPanel.vue'), 'utf8')

function createHarness() {
  let pendingFunctionItems = [{ target_file: 'scripts/a.py', purpose: 'A' }]
  let pendingResponsibilityEdges = [{ from_node: 'platform_input_node', to_node: 'scripts/a.py' }]
  let resolvedFunctionItems = null
  let resolvedResponsibilityEdges = null
  let graphPlanningActive = false
  let resolvedGraphReceivedForCurrentPlan = false
  const thoughts = []
  let activeExecutionTab = 'process'
  let showThoughts = true
  let executionPanelHasUpdate = false

  const saveResolvedGraphSnapshot = ({ functionItems, responsibilityEdges, markReceived = false } = {}) => {
    if (Array.isArray(functionItems)) resolvedFunctionItems = functionItems
    if (Array.isArray(responsibilityEdges)) resolvedResponsibilityEdges = responsibilityEdges
    if (markReceived && (Array.isArray(functionItems) || Array.isArray(responsibilityEdges))) resolvedGraphReceivedForCurrentPlan = true
    if (Array.isArray(functionItems) || Array.isArray(responsibilityEdges)) graphPlanningActive = false
  }
  const planHasPythonScript = (plan) => (Array.isArray(plan?.files) ? plan.files : []).some(file => String(file?.path || file || '').replace(/\\/g, '/').startsWith('scripts/') && String(file?.path || file || '').endsWith('.py'))
  const graphCandidateFromReadyPlan = (plan) => {
    const topFunctionItems = Array.isArray(plan?.function_items) && plan.function_items.length > 0 ? plan.function_items : null
    const topEdges = Array.isArray(plan?.responsibility_edges) ? plan.responsibility_edges : []
    if (topFunctionItems) return { functionItems: topFunctionItems, responsibilityEdges: topEdges }
    const graph = plan?.requirement_graph && typeof plan.requirement_graph === 'object' ? plan.requirement_graph : {}
    const graphRequirements = Array.isArray(graph.requirements) && graph.requirements.length > 0 ? graph.requirements : null
    return {
      functionItems: graphRequirements,
      responsibilityEdges: Array.isArray(graph.dataflow_edges) ? graph.dataflow_edges : null,
    }
  }
  const saveReadyPlanGraphSnapshot = (plan) => {
    if (resolvedGraphReceivedForCurrentPlan) return
    const { functionItems, responsibilityEdges } = graphCandidateFromReadyPlan(plan)
    if (!planHasPythonScript(plan)) return saveResolvedGraphSnapshot({ functionItems: functionItems || [], responsibilityEdges: responsibilityEdges || [] })
    if (Array.isArray(functionItems) && functionItems.length > 0) saveResolvedGraphSnapshot({ functionItems, responsibilityEdges: Array.isArray(responsibilityEdges) ? responsibilityEdges : [] })
  }
  const onPlanner = (event) => {
    if (Array.isArray(event.function_items)) pendingFunctionItems = event.function_items
    if (Array.isArray(event.responsibility_edges)) pendingResponsibilityEdges = event.responsibility_edges
    if (event.event === 'planner_converged' || event.event === 'graph_resolved') {
      saveResolvedGraphSnapshot({ functionItems: event.function_items, responsibilityEdges: event.responsibility_edges, markReceived: true })
    }
  }
  const onPlan = (plan) => {
    if (plan.status === 'ready') {
      if (Array.isArray(plan.function_items)) pendingFunctionItems = plan.function_items
      if (Array.isArray(plan.responsibility_edges)) pendingResponsibilityEdges = plan.responsibility_edges
      saveReadyPlanGraphSnapshot(plan)
    }
    graphPlanningActive = false
  }
  const markExecutionPanelUpdated = () => {
    if (showThoughts) {
      executionPanelHasUpdate = false
      return
    }
    executionPanelHasUpdate = true
  }
  const appendExecutionBlock = ({ step, label, detail = '', content = '' } = {}) => {
    const safeContent = Array.isArray(content) ? content.filter(item => typeof item === 'string').slice(0, 20) : (typeof content === 'string' ? content : '')
    thoughts.push({ step: String(step || 'execution'), label: String(label || '执行步骤'), detail: String(detail || ''), content: safeContent })
    markExecutionPanelUpdated()
  }
  const receiveExecutionEvent = (step) => appendExecutionBlock({ step, label: step })
  const clickTab = (tab) => { activeExecutionTab = tab }
  const closePanel = () => { showThoughts = false; executionPanelHasUpdate = false }
  const openPanel = () => { showThoughts = true; executionPanelHasUpdate = false }
  const onStreamEvent = (event) => {
    if (event.event === 'planner_convergence_review') {
      appendExecutionBlock({
        step: 'planner_convergence_review',
        label: '规划模型复核方案',
        detail: event.summary || '规划复核完成',
        content: Array.isArray(event.items) ? event.items : [],
      })
      return
    }
    onPlanner(event)
  }
  const startRevise = () => { graphPlanningActive = true; resolvedGraphReceivedForCurrentPlan = false }
  const displayedFunctionItems = () => (graphPlanningActive && Array.isArray(pendingFunctionItems) && pendingFunctionItems.length > 0) ? pendingFunctionItems : (Array.isArray(resolvedFunctionItems) ? resolvedFunctionItems : pendingFunctionItems)
  const clearChat = () => {
    pendingFunctionItems = []
    pendingResponsibilityEdges = []
    graphPlanningActive = false
    resolvedFunctionItems = null
    resolvedResponsibilityEdges = null
    resolvedGraphReceivedForCurrentPlan = false
    thoughts.length = 0
    activeExecutionTab = 'process'
    showThoughts = false
    executionPanelHasUpdate = false
  }
  return {
    get pendingFunctionItems() { return pendingFunctionItems },
    get pendingResponsibilityEdges() { return pendingResponsibilityEdges },
    get resolvedFunctionItems() { return resolvedFunctionItems },
    get resolvedResponsibilityEdges() { return resolvedResponsibilityEdges },
    get graphPlanningActive() { return graphPlanningActive },
    thoughts,
    get activeExecutionTab() { return activeExecutionTab },
    get executionPanelHasUpdate() { return executionPanelHasUpdate },
    clickTab,
    closePanel,
    openPanel,
    receiveExecutionEvent,
    onPlanner,
    onStreamEvent,
    onPlan,
    appendExecutionBlock,
    startRevise,
    displayedFunctionItems,
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
    h.onPlan({ status: 'ready', files: [{ path: 'scripts/a.py' }], function_items: [{ target_file: 'scripts/a.py' }], responsibility_edges: [{ from_node: 'scripts/a.py' }] })
    h.appendExecutionBlock({ step: 'file_generation_start', label: '文件开始生成' })
    h.appendExecutionBlock({ step: 'creation_complete', label: '创建完成' })
    assert.equal(h.resolvedFunctionItems.length, 1)
    assert.equal(h.resolvedResponsibilityEdges.length, 1)
  })

  it('only clearChat clears graph state', () => {
    const h = createHarness()
    h.onPlan({ status: 'ready', files: [{ path: 'scripts/a.py' }], function_items: [{ target_file: 'scripts/a.py' }], responsibility_edges: [{ from_node: 'scripts/a.py' }] })
    h.clearChat()
    assert.deepEqual(h.pendingFunctionItems, [])
    assert.deepEqual(h.pendingResponsibilityEdges, [])
    assert.equal(h.resolvedFunctionItems, null)
    assert.equal(h.resolvedResponsibilityEdges, null)
  })

  it('does not add user_input cards to execution process', () => {
    assert.equal(creatorSource.includes("step: 'user_input'"), false)
  })

  it('keeps graph tab active across execution updates and closed panel badge state', () => {
    const h = createHarness()
    h.clickTab('graph')
    for (const step of ['file_generation_start', 'validate_start', 'repair_start', 'e2e_start', 'package_start']) {
      h.receiveExecutionEvent(step)
      assert.equal(h.activeExecutionTab, 'graph')
    }
    assert.equal(h.thoughts.length, 5)
    h.closePanel()
    h.receiveExecutionEvent('file_generation_done')
    assert.equal(h.executionPanelHasUpdate, true)
    h.openPanel()
    assert.equal(h.activeExecutionTab, 'graph')
  })

  it('shows pending draft during revise while retaining the previous resolved snapshot', () => {
    const h = createHarness()
    const oldResolved = [{ target_file: 'scripts/old.py' }]
    const newDraft = [{ target_file: 'scripts/new.py' }]
    h.onPlan({ status: 'ready', files: [{ path: 'scripts/old.py' }], function_items: oldResolved, responsibility_edges: [{ from_node: 'scripts/old.py' }] })
    h.startRevise()
    h.onPlanner({ event: 'planner_draft', function_items: newDraft })
    assert.deepEqual(h.displayedFunctionItems(), newDraft)
    assert.deepEqual(h.resolvedFunctionItems, oldResolved)
  })

  it('keeps previous resolved graph when revise final plan still needs clarification with empty arrays', () => {
    const h = createHarness()
    const oldResolved = [{ target_file: 'scripts/old.py' }]
    h.onPlan({ status: 'ready', files: [{ path: 'scripts/old.py' }], function_items: oldResolved, responsibility_edges: [{ from_node: 'scripts/old.py' }] })
    h.startRevise()
    h.onPlan({ status: 'needs_clarification', function_items: [], responsibility_edges: [] })
    assert.deepEqual(h.resolvedFunctionItems, oldResolved)
    assert.equal(h.graphPlanningActive, false)
    assert.deepEqual(h.displayedFunctionItems(), oldResolved)
  })

  it('saves explicit empty ready graph for scriptless skills', () => {
    const h = createHarness()
    h.onPlan({ status: 'ready', function_items: [{ target_file: 'scripts/old.py' }], responsibility_edges: [{ from_node: 'scripts/old.py' }] })
    h.startRevise()
    h.onPlan({ status: 'ready', function_items: [], responsibility_edges: [] })
    assert.deepEqual(h.resolvedFunctionItems, [])
    assert.deepEqual(h.resolvedResponsibilityEdges, [])
    assert.deepEqual(h.displayedFunctionItems(), [])
  })

  it('adds sanitized planner convergence review cards without saving raw responses or full plans', () => {
    const h = createHarness()
    h.onStreamEvent({
      event: 'planner_convergence_review',
      summary: '发现 1 个需调整点',
      items: ['补齐输出交付说明'],
      raw: { hidden: true },
      response: { full: true },
      plan: { full: true },
    })
    assert.equal(h.thoughts.length, 1)
    assert.equal(h.thoughts[0].step, 'planner_convergence_review')
    assert.deepEqual(h.thoughts[0], {
      step: 'planner_convergence_review',
      label: '规划模型复核方案',
      detail: '发现 1 个需调整点',
      content: ['补齐输出交付说明'],
    })
    assert.equal(JSON.stringify(h.thoughts).includes('hidden'), false)
    assert.equal(JSON.stringify(h.thoughts).includes('full'), false)
  })

  it('CreatorExecutionPanel passes content-only to ThinkingPanel', () => {
    assert.match(creatorExecutionPanelSource, /<ThinkingPanel[^>]*:thoughts="thoughts"[^>]*content-only/)
  })

  it('ThinkingPanel hides thought.data fallback when contentOnly is true', () => {
    assert.match(thinkingSource, /contentOnly/)
    assert.match(thinkingSource, /v-else-if="!contentOnly"[^>]*>\{\{ JSON\.stringify\(thought\.data, null, 2\) \}\}/)
  })

  it('ThinkingPanel default mode still supports Sandbox thought.data fallback', () => {
    assert.match(thinkingSource, /contentOnly:\s*\{[\s\S]*default:\s*false/)
    assert.match(thinkingSource, /JSON\.stringify\(thought\.data, null, 2\)/)
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

describe('CreatorView current planning graph snapshot rules', () => {
  it('keeps resolved graph visible when send starts and pending is empty', () => {
    const h = createHarness()
    const resolved = [{ target_file: 'scripts/stable.py' }]
    h.onPlan({ status: 'ready', files: [{ path: 'scripts/stable.py' }], function_items: resolved, responsibility_edges: [] })
    h.startRevise()
    h.onPlanner({ event: 'planner_draft', function_items: [], responsibility_edges: [] })
    assert.deepEqual(h.displayedFunctionItems(), resolved)
  })

  it('graph_resolved snapshot is not overwritten by empty ready top-level arrays', () => {
    const h = createHarness()
    const resolved = [{ target_file: 'scripts/new.py' }]
    h.startRevise()
    h.onPlanner({ event: 'graph_resolved', function_items: resolved, responsibility_edges: [] })
    h.onPlan({ status: 'ready', files: [{ path: 'scripts/new.py' }], function_items: [], responsibility_edges: [] })
    assert.deepEqual(h.resolvedFunctionItems, resolved)
  })

  it('ready plan can backfill from requirement_graph when no resolved event arrived', () => {
    const h = createHarness()
    h.startRevise()
    h.onPlan({ status: 'ready', files: [{ path: 'scripts/fallback.py' }], requirement_graph: { requirements: [{ target_file: 'scripts/fallback.py' }], dataflow_edges: [] } })
    assert.deepEqual(h.resolvedFunctionItems, [{ target_file: 'scripts/fallback.py' }])
  })

  it('empty top-level ready graph arrays do not block requirement_graph fallback', () => {
    const h = createHarness()
    h.startRevise()
    h.onPlan({ status: 'ready', files: [{ path: 'scripts/fallback.py' }], function_items: [], responsibility_edges: [], requirement_graph: { requirements: [{ target_file: 'scripts/fallback.py' }], dataflow_edges: [{ from_node: 'platform_input_node', to_node: 'scripts/fallback.py' }] } })
    assert.deepEqual(h.resolvedFunctionItems, [{ target_file: 'scripts/fallback.py' }])
    assert.deepEqual(h.resolvedResponsibilityEdges, [{ from_node: 'platform_input_node', to_node: 'scripts/fallback.py' }])
  })

  it('ready plan with scripts and empty candidate preserves existing resolved graph', () => {
    const h = createHarness()
    const oldResolved = [{ target_file: 'scripts/old.py' }]
    h.onPlan({ status: 'ready', files: [{ path: 'scripts/old.py' }], function_items: oldResolved, responsibility_edges: [] })
    h.startRevise()
    h.onPlan({ status: 'ready', files: [{ path: 'scripts/new.py' }], function_items: [], responsibility_edges: [] })
    assert.deepEqual(h.resolvedFunctionItems, oldResolved)
  })
})
