<template>
  <aside class="event-stream" aria-label="Creator 事件流" aria-live="polite">
    <header><div><small>ACTIVITY</small><h3>事件流</h3></div><span>{{ events.length }}</span></header>
    <div v-if="events.length" ref="listEl" class="event-list">
      <article v-for="(event, index) in events" :key="event.id || index" :class="event.level || 'success'">
        <time>{{ event.time || '--:--:--' }}</time>
        <span class="event-marker">{{ icon(event.level) }}</span>
        <div><strong>{{ event.label }}</strong><p v-if="event.detail">{{ event.detail }}</p><ul v-if="items(event).length"><li v-for="item in items(event)" :key="item">{{ item }}</li></ul></div>
      </article>
    </div>
    <p v-else class="empty-events">开始后将在这里显示需求解析、图谱、文件与 Runtime 事件。</p>
  </aside>
</template>
<script setup>
import { nextTick, ref, watch } from 'vue'
const props = defineProps({ events: { type: Array, default: () => [] } })
const listEl = ref(null)
function icon(level) { return ({ success: '✓', running: '●', warning: '!', failed: '×' })[level] || '✓' }
function items(event) { return (Array.isArray(event.content) ? event.content : event.content ? [event.content] : []).slice(0, 5) }
watch(() => props.events.length, async () => { await nextTick(); if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight })
</script>
<style scoped>
.event-stream { display: flex; width: 280px; min-width: 240px; min-height: 0; flex-direction: column; border-right: 1px solid var(--border); background: var(--surface); overflow: hidden; }.event-stream header { display: flex; align-items: center; justify-content: space-between; padding: 14px 16px 10px; border-bottom: 1px solid var(--border); }.event-stream h3 { margin: 2px 0 0; font-size: 15px; }.event-stream small { color: #2563eb; font-size: 9px; font-weight: 800; letter-spacing: .15em; }.event-stream header > span { padding: 2px 7px; border-radius: 999px; background: var(--surface2); font-size: 11px; }.event-list { min-height: 0; padding: 8px 12px 24px; overflow-y: auto; }.event-list article { display: grid; grid-template-columns: 52px 20px minmax(0, 1fr); gap: 6px; padding: 10px 0; border-bottom: 1px solid var(--border); }.event-list time { padding-top: 2px; color: var(--text-muted); font-family: monospace; font-size: 10px; }.event-marker { display: grid; width: 18px; height: 18px; place-items: center; border-radius: 50%; background: #dcfce7; color: #166534; font-size: 10px; font-weight: 900; }.running .event-marker { background: #dbeafe; color: #1d4ed8; animation: pulse 1.4s infinite; }.warning .event-marker { background: #fef3c7; color: #92400e; }.failed .event-marker { background: #fee2e2; color: #991b1b; }.event-list strong { display: block; font-size: 12px; }.event-list p { margin: 3px 0 0; color: var(--text-muted); font-size: 11px; line-height: 1.45; }.event-list ul { margin: 6px 0 0; padding-left: 14px; color: var(--text-muted); font-size: 10px; line-height: 1.5; }.empty-events { margin: auto; padding: 20px; color: var(--text-muted); font-size: 12px; line-height: 1.6; text-align: center; }@keyframes pulse { 50% { opacity: .45; } }
@media (max-width: 1050px) { .event-stream { width: 220px; min-width: 200px; } }@media (max-width: 900px) { .event-stream { width: 100%; min-width: 0; max-height: 210px; border-right: 0; border-bottom: 1px solid var(--border); }.event-list { display: flex; gap: 12px; overflow: auto; }.event-list article { min-width: 250px; } }
</style>
