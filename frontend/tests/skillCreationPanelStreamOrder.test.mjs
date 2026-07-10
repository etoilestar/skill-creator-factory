import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const panelSource = readFileSync(resolve(__dirname, '../src/components/SkillCreationPanel.vue'), 'utf8')

function generateOneFileBranchBody() {
  const start = panelSource.indexOf('async function generateOneFile(idx)')
  assert.notEqual(start, -1, 'generateOneFile must exist')
  const loop = panelSource.indexOf('for await (const chunk of generateFileStream', start)
  assert.notEqual(loop, -1, 'generateOneFile must consume generateFileStream')
  const end = panelSource.indexOf('if (!generationSucceeded)', loop)
  assert.notEqual(end, -1, 'generateOneFile must keep explicit success validation')
  return panelSource.slice(loop, end)
}

function simulateChunkHandling(chunks, initialContent = '') {
  const file = {
    path: 'references/example.md',
    required: false,
    status: 'generating',
    generatedContent: initialContent,
    repairMessage: '',
    showPreview: false,
    error: '',
  }
  let generationSucceeded = false

  for (const chunk of chunks) {
    if (chunk?.fileDone) {
      if (typeof chunk.content === 'string' && chunk.content) {
        file.generatedContent = chunk.content
      }
      if (chunk.success === true && chunk.status === 'success') {
        generationSucceeded = true
        break
      }
      if (chunk.needsRepair || chunk.success === false || chunk.status === 'needs_repair' || chunk.validationStatus === 'needs_repair') {
        file.status = 'needs_repair'
        file.showPreview = true
        file.error = chunk.error || '生成结果需要人工修复'
        return { file, generationSucceeded }
      }
      file.status = 'error'
      file.error = chunk.error || '生成流结束但未返回成功终态'
      return { file, generationSucceeded }
    } else if (chunk?.done) {
      break
    } else if (chunk?.validation) {
      file.repairMessage = 'validation handled'
    } else if (chunk?.error) {
      if (typeof chunk.content === 'string' && chunk.content.trim()) {
        file.generatedContent = chunk.content
      }
      file.status = 'error'
      file.error = chunk.error
      return { file, generationSucceeded }
    } else if (typeof chunk === 'string') {
      file.generatedContent += chunk
    } else if (typeof chunk?.content === 'string') {
      file.generatedContent += chunk.content
    } else if (typeof chunk?.delta === 'string') {
      file.generatedContent += chunk.delta
    }
  }

  if (!generationSucceeded) {
    file.status = 'error'
    file.error = '生成流已结束，但后端未返回显式成功终态'
    return { file, generationSucceeded }
  }

  if (!file.generatedContent.trim()) {
    file.status = 'error'
    file.error = '模型未返回任何内容，请重试或手动填写'
    return { file, generationSucceeded }
  }

  file.status = 'preview'
  return { file, generationSucceeded }
}

describe('SkillCreationPanel generateOneFile stream branch ordering', () => {
  it('keeps structured control events before generic content and delta handlers', () => {
    const body = generateOneFileBranchBody()
    const positions = [
      'if (chunk?.fileDone)',
      '} else if (chunk?.done)',
      '} else if (chunk?.validation)',
      '} else if (chunk?.error)',
      "} else if (typeof chunk === 'string')",
      "} else if (typeof chunk?.content === 'string')",
      "} else if (typeof chunk?.delta === 'string')",
    ].map((needle) => {
      const index = body.indexOf(needle)
      assert.notEqual(index, -1, `${needle} must exist in generateOneFile chunk handling`)
      return index
    })

    assert.deepEqual([...positions].sort((a, b) => a - b), positions)
  })

  it('recognizes fileDone success with empty content as terminal success', () => {
    const { file, generationSucceeded } = simulateChunkHandling([
      'generated body',
      { fileDone: true, success: true, status: 'success', content: '' },
    ])

    assert.equal(generationSucceeded, true)
    assert.equal(file.status, 'preview')
    assert.equal(file.generatedContent, 'generated body')
  })

  it('recognizes fileDone success with non-empty content and preserves terminal content', () => {
    const { file, generationSucceeded } = simulateChunkHandling([
      'draft body',
      { fileDone: true, success: true, status: 'success', content: 'terminal body' },
    ])

    assert.equal(generationSucceeded, true)
    assert.equal(file.status, 'preview')
    assert.equal(file.generatedContent, 'terminal body')
  })

  it('routes error events with empty content to the error branch', () => {
    const { file, generationSucceeded } = simulateChunkHandling([
      'partial body',
      { error: 'boom', content: '' },
    ])

    assert.equal(generationSucceeded, false)
    assert.equal(file.status, 'error')
    assert.equal(file.error, 'boom')
    assert.equal(file.generatedContent, 'partial body')
  })

  it('continues to append ordinary string, content, and delta chunks', () => {
    const { file } = simulateChunkHandling([
      'a',
      { content: 'b' },
      { delta: 'c' },
      { fileDone: true, success: true, status: 'success', content: '' },
    ])

    assert.equal(file.status, 'preview')
    assert.equal(file.generatedContent, 'abc')
  })

  it('leaves a successful optional reference in preview instead of skipped', () => {
    const { file } = simulateChunkHandling([
      { content: 'reference notes' },
      { fileDone: true, success: true, status: 'success', content: '' },
    ])

    assert.equal(file.required, false)
    assert.equal(file.status, 'preview')
    assert.notEqual(file.status, 'skipped')
  })
})
