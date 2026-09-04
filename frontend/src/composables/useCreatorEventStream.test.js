import test from 'node:test'
import assert from 'node:assert/strict'
import { useCreatorEventStream } from './useCreatorEventStream.js'
import { normalizeCreatorEvent } from '../types/creator-events.js'

test('normalizes backend graph envelopes into the canonical model', () => {
  const event = normalizeCreatorEvent({ type: 'graph_update', payload: { status: 'success', nodes_created: 20, edges_created: 35 } })
  assert.equal(event.stage, 'graph')
  assert.equal(event.level, 'success')
  assert.equal(event.payload.nodes_created, 20)
})

test('projects lifecycle, detail and artifact views without leaking detail to summary', () => {
  const stream = useCreatorEventStream()
  stream.receive({ event: 'graph_resolved', detail: '20 nodes', nodes_created: 20 })
  stream.receive({ phase: 'e2e_failed', detail: 'bad input', failure_summary: 'CSV header missing' })
  assert.equal(stream.summaryEvents.value.length, 2)
  assert.deepEqual(stream.summaryEvents.value.map(item => item.message), ['', ''])
  assert.equal(stream.detailEvents.value.length, 2)
  assert.equal(stream.artifactEvents.value.length, 2)
})
