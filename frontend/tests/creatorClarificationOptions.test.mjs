import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { extractClarificationQuestionOptions } from '../src/composables/useCreator.js'

describe('extractClarificationQuestionOptions', () => {
  it('parses options containing PDF/DOCX/TXT, JSON, and Markdown without dropping letters', () => {
    const options = extractClarificationQuestionOptions(
      '需要支持哪些输入文件类型和输出格式？A. 支持 PDF/DOCX/TXT（推荐） B. 严格 JSON C. JSON + 可读 Markdown D. 只要可读文本'
    )

    assert.deepEqual(options.map((option) => option.text), [
      'A. 支持 PDF/DOCX/TXT（推荐）',
      'B. 严格 JSON',
      'C. JSON + 可读 Markdown',
      'D. 只要可读文本',
    ])
    assert.match(options[0].value, /问题：需要支持哪些输入文件类型和输出格式？/)
    assert.match(options[0].value, /选择：A\. 支持 PDF\/DOCX\/TXT（推荐）/)
    assert.match(options[2].value, /Markdown/)
  })
})
