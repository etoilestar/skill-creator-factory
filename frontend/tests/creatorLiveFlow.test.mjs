import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const creator = readFileSync(resolve(here, '../src/views/CreatorView.vue'), 'utf8')
const creation = readFileSync(resolve(here, '../src/components/SkillCreationPanel.vue'), 'utf8')

describe('Creator live main-flow rendering', () => {
  it('applies streamed blueprint text before the complete plan arrives', () => {
    const callback = creator.slice(creator.indexOf('const plan = await streamPrepareCreationPlan'), creator.indexOf("if (event.event === 'planner_convergence_review')"))
    assert.match(callback, /event\.blueprint_text/)
    assert.match(callback, /pendingBlueprintText\.value = event\.blueprint_text\.trim\(\)/)
    assert.match(callback, /nextTick\(scrollBottom\)/)
  })
  it('renders graph progress below the planning reveal in document order', () => {
    assert.ok(creator.indexOf('class="planning-reveal"') < creator.indexOf('class="live-process-area"'))
  })
  it('renders every runtime transport event in an aria-live feed', () => {
    assert.match(creation, /runtimeActivityRows\.length[^>]*class="runtime-live-feed" aria-live="polite"/)
    assert.match(creation, /runtimeEvents\.value\.map/)
  })
})
