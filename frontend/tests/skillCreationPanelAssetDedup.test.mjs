import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(resolve(__dirname, '../src/components/SkillCreationPanel.vue'), 'utf8')

function loadMergeHelper() {
  const start = source.indexOf('function normalizeCreationFilePath')
  const end = source.indexOf('\nconst localFiles = ref(', start)
  assert.notEqual(start, -1)
  assert.notEqual(end, -1)
  return Function(`${source.slice(start, end)}; return mergeCreationFilesByPath`)()
}

describe('SkillCreationPanel creation file identity', () => {
  it('merges projection metadata by normalized exact path', () => {
    const merge = loadMergeHelper()
    const files = merge([
      { path: './assets/a.bin', purpose: 'planned file', required: false },
      { path: 'assets\\a.bin', uploaded_provided: true, asset_source: 'user_upload' },
      { path: 'assets/a.bin', asset_requirement: true, required: true },
    ])

    assert.deepEqual(files, [{
      path: 'assets/a.bin',
      purpose: 'planned file',
      required: true,
      uploaded_provided: true,
      asset_requirement: true,
      asset_source: 'user_upload',
    }])
  })

  it('does not merge distinct paths that share a basename', () => {
    const merge = loadMergeHelper()
    assert.equal(merge([{ path: 'assets/a.bin' }, { path: 'references/a.bin' }]).length, 2)
  })
})
