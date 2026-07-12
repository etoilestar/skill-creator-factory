import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const creatorSource = readFileSync(resolve(__dirname, '../src/views/CreatorView.vue'), 'utf8')
const panelSource = readFileSync(resolve(__dirname, '../src/components/SkillCreationPanel.vue'), 'utf8')
const composableSource = readFileSync(resolve(__dirname, '../src/composables/useCreator.js'), 'utf8')

describe('Creator responsibility_edges generate-file plumbing', () => {
  it('passes creationPlan.responsibility_edges into SkillCreationPanel', () => {
    assert.match(creatorSource, /:responsibility-edges="creationPlan\.responsibility_edges \|\| \[\]"/)
  })

  it('SkillCreationPanel declares responsibilityEdges prop and forwards it to generateFileStream', () => {
    assert.match(panelSource, /responsibilityEdges:\s*\{\s*type:\s*Array,\s*default:\s*\(\) => \[\]/)
    assert.match(panelSource, /responsibilityEdges:\s*props\.responsibilityEdges/)
  })

  it('generateFileStream accepts responsibilityEdges and sends responsibility_edges request body', () => {
    assert.match(composableSource, /responsibilityEdges\s*=\s*\[\]/)
    assert.match(composableSource, /responsibility_edges:\s*responsibilityEdges/)
  })
})
