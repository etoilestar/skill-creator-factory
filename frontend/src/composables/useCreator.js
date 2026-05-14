/**
 * useCreator — composable for the frontend-driven Skill creation flow (plan C).
 *
 * All API calls are routed through /api/creator/* which is handled by
 * backend/routers/creator.py.
 */

/**
 * Analyze the conversation blueprint and extract a file-creation plan.
 * This is a pure rule-based call — no LLM is involved.
 *
 * @param {Array<{role:string, content:string}>} messages - full conversation history
 * @param {string|null} [model] - optional model name (ignored by the endpoint but kept for symmetry)
 * @returns {Promise<{skill_name:string, files:Array, warnings:string[]}>}
 */
export async function analyzeBlueprintPlan(messages, model = null) {
  const resp = await fetch('/api/creator/analyze-blueprint', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, model }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || '蓝图解析请求失败')
  }
  return resp.json()
}

/**
 * Initialise a new Skill directory structure on the backend.
 *
 * @param {string} skillName
 * @returns {Promise<{success:boolean, path:string|null, message:string}>}
 */
export async function initSkill(skillName) {
  const resp = await fetch('/api/creator/init-skill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ skill_name: skillName }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || '目录初始化失败')
  }
  return resp.json()
}

/**
 * Stream the generated content for a single Skill file.
 *
 * Yields:
 *   - string chunks while the model is generating
 *   - { done: true } when generation is complete
 *   - { error: string } on failure
 *
 * @param {{
 *   skillName: string,
 *   filePath: string,
 *   purpose: string,
 *   blueprintText: string,
 *   conversationHistory: Array,
 *   model?: string|null
 * }} params
 * @yields {string | {done:true} | {error:string}}
 */
export async function* generateFileStream({
  skillName,
  filePath,
  purpose,
  blueprintText,
  conversationHistory,
  model = null,
}) {
  const resp = await fetch('/api/creator/generate-file', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      skill_name: skillName,
      file_path: filePath,
      purpose,
      blueprint_text: blueprintText,
      conversation_history: conversationHistory,
      model,
    }),
  })

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || '文件内容生成请求失败')
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() // keep incomplete last line

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      const raw = line.slice(6).trim()
      if (raw === '[DONE]') return
      try {
        const parsed = JSON.parse(raw)
        if (parsed.error) {
          yield { error: parsed.error }
          return
        }
        if (parsed.done) {
          yield { done: true }
          return
        }
        if (typeof parsed.content === 'string') {
          yield parsed.content
        }
      } catch {
        // skip unparseable lines
      }
    }
  }
}

/**
 * Write the final file content to disk.
 * The backend automatically strips any spurious code-fence wrapping.
 *
 * @param {string} skillName
 * @param {string} filePath  - e.g. "SKILL.md" or "scripts/main.py"
 * @param {string} content
 * @returns {Promise<{success:boolean, path:string|null, bytes:number, message:string}>}
 */
export async function writeFile(skillName, filePath, content) {
  const resp = await fetch('/api/creator/write-file', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ skill_name: skillName, file_path: filePath, content }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || '文件写入失败')
  }
  return resp.json()
}

/**
 * Validate the SKILL.md of a Skill package.
 *
 * @param {string} skillName
 * @returns {Promise<{success:boolean, path:string|null, message:string}>}
 */
export async function validateSkill(skillName) {
  const resp = await fetch('/api/creator/validate-skill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ skill_name: skillName }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || '校验请求失败')
  }
  return resp.json()
}

/**
 * Package a Skill directory into a distributable archive.
 *
 * @param {string} skillName
 * @returns {Promise<{success:boolean, path:string|null, message:string}>}
 */
export async function packageSkill(skillName) {
  const resp = await fetch('/api/creator/package-skill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ skill_name: skillName }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    throw new Error(err.detail || '打包请求失败')
  }
  return resp.json()
}
