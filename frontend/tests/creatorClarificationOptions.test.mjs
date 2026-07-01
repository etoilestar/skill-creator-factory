import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { buildClarificationQuickActions, extractClarificationQuestionOptions } from '../src/composables/useCreator.js'

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

  it('builds quick actions for only one displayed question and includes question context', () => {
    const actions = buildClarificationQuickActions([
      '输出格式希望是哪种？A. 严格 JSON B. JSON + 可读 Markdown（推荐） C. 只要可读文本',
    ])

    assert.equal(actions.length, 3)
    assert.match(actions[1].value, /^问题：输出格式希望是哪种？/)
    assert.match(actions[1].value, /选择：B\. JSON \+ 可读 Markdown（推荐）/)
  })

  it('waits for input only on affirmative supplement choices', () => {
    const actions = extractClarificationQuestionOptions(
      '还有其他需要补充的要求吗？A. 没有，按上面的选择继续 B. 有，我补充说明'
    )

    assert.equal(actions[0].waitForInput, false)
    assert.equal(actions[1].waitForInput, true)
  })
})
