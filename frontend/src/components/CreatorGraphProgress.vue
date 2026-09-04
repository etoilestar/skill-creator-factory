<template>
  <section class="progress-card" aria-live="polite">
    <header><div><small>LIVE GRAPH</small><h3>责任图谱构建</h3></div><span :class="status">{{ statusText }}</span></header>
    <div class="metrics"><div><strong>{{ nodeCount }}</strong><span>节点</span></div><div><strong>{{ edgeCount }}</strong><span>边</span></div></div>
    <p class="operation"><span class="pulse" />{{ currentOperation }}</p>
    <ol><li v-for="step in steps" :key="step.label" :class="step.state"><i />{{ step.label }}</li></ol>
  </section>
</template>
<script setup>
import { computed } from 'vue'
const props = defineProps({ events: { type: Array, default: () => [] }, nodes: { type: Array, default: () => [] }, edges: { type: Array, default: () => [] } })
const graphEvents = computed(() => props.events.filter(event => event.stage === 'graph' || event.stage === 'repair'))
const latest = computed(() => graphEvents.value.at(-1))
const numberFrom = (keys, fallback) => { for (const event of [...graphEvents.value].reverse()) for (const key of keys) { const value = event.detail?.[key] ?? event.payload?.[key]; if (Number.isFinite(Number(value))) return Number(value) } return fallback }
const nodeCount = computed(() => numberFrom(['nodes_created', 'node_count'], props.nodes.length))
const edgeCount = computed(() => numberFrom(['edges_created', 'edge_count'], props.edges.length))
const currentOperation = computed(() => latest.value?.message || latest.value?.title || '等待图谱规划')
const status = computed(() => latest.value?.level || 'info')
const statusText = computed(() => ({ info: '构建中', success: '已完成', warning: '修复中', error: '需处理' })[status.value])
const labels = ['Graph planning', 'Node creation', 'Edge creation', 'Validation', 'Repair']
const steps = computed(() => labels.map((label, index) => ({ label, state: graphEvents.value.some(event => new RegExp(label.split(' ')[0], 'i').test(`${event.title} ${event.payload?.type || ''}`)) || index < Math.min(graphEvents.value.length, 4) ? 'done' : (index === Math.min(graphEvents.value.length, 4) ? 'active' : '') })))
</script>
<style scoped>
.progress-card{padding:16px;border:1px solid var(--border);border-radius:12px;background:var(--surface)}header{display:flex;justify-content:space-between;align-items:center}h3{margin:2px 0;font-size:15px}small{color:#2563eb;font-weight:800;letter-spacing:.12em}header>span{font-size:11px;padding:3px 8px;border-radius:999px;background:#dbeafe;color:#1d4ed8}.success{background:#dcfce7;color:#166534}.warning{background:#fef3c7;color:#92400e}.error{background:#fee2e2;color:#991b1b}.metrics{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:14px 0}.metrics div{padding:10px;border-radius:9px;background:var(--surface2);text-align:center}.metrics strong,.metrics span{display:block}.metrics strong{font-size:22px;color:#2563eb}.metrics span{font-size:11px;color:var(--text-muted)}.operation{font-size:12px}.pulse{display:inline-block;width:7px;height:7px;margin-right:7px;border-radius:50%;background:#3b82f6}ol{display:flex;gap:5px;padding:0;list-style:none;flex-wrap:wrap}li{font-size:10px;color:var(--text-muted)}li:not(:last-child)::after{content:' →';margin-left:5px}.done{color:#166534}.active{color:#1d4ed8;font-weight:700}
</style>
