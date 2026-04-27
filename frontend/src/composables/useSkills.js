/**
 * Skills API helpers.
 */

export async function fetchSkills() {
  const res = await fetch('/api/skills')
  if (!res.ok) throw new Error(`Failed to fetch skills: ${res.statusText}`)
  return res.json()
}

export async function fetchSkill(name) {
  const res = await fetch(`/api/skills/${encodeURIComponent(name)}`)
  if (!res.ok) throw new Error(`Skill not found: ${name}`)
  return res.json()
}

export async function saveSkill(name, content) {
  const res = await fetch('/api/skills', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, content }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Save failed')
  }
  return res.json()
}

export async function deleteSkill(name) {
  const res = await fetch(`/api/skills/${encodeURIComponent(name)}`, { method: 'DELETE' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Delete failed')
  }
  return res.json()
}

export async function fetchLlmHealth() {
  const res = await fetch('/api/health/llm')
  if (!res.ok) throw new Error('Health check failed')
  return res.json()
}
