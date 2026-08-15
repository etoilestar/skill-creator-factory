/** Parse additive Sandbox SSE events without affecting shared chat composables. */
export function applySandboxEvent(state, event) {
  if (!event || typeof event === 'string') return
  const value = event.data
  if (event.type === 'result_manifest') state.resultManifest = value
  else if (event.type === 'plan_preview') state.planPreview = value
  else if (event.type === 'thought') state.thoughts.push(value)
  else if (event.type === 'task_progress') state.taskProgress = value
  else if (event.type === 'sandbox_retry') state.retries.push(value)
}
