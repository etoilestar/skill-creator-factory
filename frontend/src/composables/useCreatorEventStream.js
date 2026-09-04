import { computed, ref } from 'vue'
import { normalizeCreatorEvent } from '../types/creator-events.js'

const SUMMARY_TITLES = {
  requirement: '需求解析', blueprint: '蓝图生成', graph: '责任图谱构建',
  generation: 'Skill 生成', e2e: 'E2E 验证', repair: '自动修复',
}

export function useCreatorEventStream() {
  const events = ref([])

  function receive(raw) {
    const incoming = Array.isArray(raw?.events) ? raw.events : [raw]
    for (const item of incoming.filter(Boolean)) {
      const event = normalizeCreatorEvent(item, events.value.length)
      if (!events.value.some(existing => existing.id === event.id)) events.value.push(event)
    }
  }

  const summaryEvents = computed(() => {
    const latest = new Map()
    for (const event of events.value) latest.set(event.stage, event)
    return [...latest.values()].map(event => ({
      ...event,
      title: `${SUMMARY_TITLES[event.stage] || event.title}${event.level === 'success' ? '完成' : event.level === 'error' ? '失败' : '中'}`,
      message: '', detail: {}, payload: {},
    }))
  })
  const detailEvents = computed(() => events.value)
  const artifactEvents = computed(() => events.value.filter(event => ['graph', 'generation', 'e2e', 'repair'].includes(event.stage)))

  function clear() { events.value = [] }
  return { events, summaryEvents, detailEvents, artifactEvents, receive, clear }
}
