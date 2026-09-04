export const CREATOR_EVENT_STAGES = Object.freeze([
  'requirement', 'blueprint', 'graph', 'generation', 'e2e', 'repair',
])

export const CREATOR_EVENT_LEVELS = Object.freeze([
  'info', 'success', 'warning', 'error',
])

const STAGE_PATTERNS = [
  ['repair', /repair|修复|retry|重试|patch/i],
  ['e2e', /e2e|runtime|validate|validation|测试|校验/i],
  ['generation', /file|generation|write|package|artifact|文件|打包/i],
  ['graph', /graph|planner|node|edge|图谱|节点|边/i],
  ['blueprint', /blueprint|蓝图/i],
  ['requirement', /requirement|intent|convergence|需求|意图/i],
]

function stageOf(raw, key) {
  const explicit = String(raw?.stage || raw?.payload?.stage || '').toLowerCase()
  if (CREATOR_EVENT_STAGES.includes(explicit)) return explicit
  return STAGE_PATTERNS.find(([, pattern]) => pattern.test(key))?.[0] || 'requirement'
}

function levelOf(raw, key) {
  const explicit = String(raw?.level || raw?.status || '').toLowerCase()
  if (['failed', 'failure'].includes(explicit)) return 'error'
  if (CREATOR_EVENT_LEVELS.includes(explicit)) return explicit
  if (/fail|error|blocked|失败|错误/i.test(key)) return 'error'
  if (/warning|repair|retry|警告|修复/i.test(key)) return 'warning'
  if (/complete|success|ready|resolved|converged|完成|成功|确认/i.test(key)) return 'success'
  return 'info'
}

/** Normalize every Creator transport shape at one boundary. */
export function normalizeCreatorEvent(raw = {}, sequence = 0) {
  const envelope = raw?.type && raw?.payload && typeof raw.payload === 'object'
    ? { ...raw.payload, type: raw.type }
    : raw
  const key = String(envelope.event || envelope.type || envelope.phase || envelope.step || 'creator_event')
  const timestamp = envelope.timestamp || new Date().toISOString()
  return {
    id: String(envelope.id || `${key}-${timestamp}-${sequence}`),
    stage: stageOf(envelope, key),
    level: levelOf(envelope, key),
    title: String(envelope.title || envelope.label || key.replaceAll('_', ' ')),
    message: String(envelope.message || envelope.summary || envelope.detail || ''),
    detail: envelope.detail && typeof envelope.detail === 'object' ? envelope.detail : {},
    timestamp,
    payload: { ...envelope, ...(envelope.payload && typeof envelope.payload === 'object' ? envelope.payload : {}) },
  }
}
