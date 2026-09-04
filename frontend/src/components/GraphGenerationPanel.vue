<template>
  <section class="generation-panel" aria-label="责任图谱生成过程">
    <header><div><small>GRAPH GENERATION</small><h4>责任图谱创建过程</h4></div><span :class="streaming ? 'running' : 'success'">{{ streaming ? '生成中' : '已同步' }}</span></header>
    <ol>
      <li :class="nodes.length ? 'success' : activeClass(1)"><b>1</b><div><strong>生成节点</strong><p v-if="nodes.length">已创建 {{ nodes.length }} 个 Function Node</p><code v-for="node in nodes.slice(-3)" :key="node.target_file">＋ {{ node.target_file }}</code></div></li>
      <li :class="edges.length ? 'success' : activeClass(2)"><b>2</b><div><strong>生成接口关系</strong><p v-if="edges.length">已建立 {{ edges.length }} 条数据流</p><code v-for="(edge, index) in edges.slice(-2)" :key="index">{{ edge.from_node }} → {{ edge.to_node }}</code></div></li>
      <li :class="resolved ? 'success' : activeClass(3)"><b>3</b><div><strong>合同检查</strong><p>input / output compatibility</p></div></li>
      <li :class="repairEvents.length ? 'warning' : (resolved ? 'success' : 'pending')"><b>4</b><div><strong>修复过程</strong><template v-if="repairEvents.length"><p v-for="item in repairEvents.slice(-2)" :key="item.detail">{{ item.detail }}</p></template><p v-else>{{ resolved ? '未发现需要修复的问题' : '等待合同检查' }}</p></div></li>
    </ol>
  </section>
</template>
<script setup>
import { computed } from 'vue'
const props = defineProps({ nodes: { type: Array, default: () => [] }, edges: { type: Array, default: () => [] }, events: { type: Array, default: () => [] }, streaming: Boolean })
const resolved = computed(() => props.events.some(item => item.step === 'graph_resolved'))
const repairEvents = computed(() => props.events.filter(item => /repair|adjust/i.test(item.step || '')))
function activeClass(step) { if (!props.streaming) return 'pending'; if (step === 1 && !props.nodes.length) return 'running'; if (step === 2 && props.nodes.length && !props.edges.length) return 'running'; if (step === 3 && props.edges.length) return 'running'; return 'pending' }
</script>
<style scoped>
.generation-panel { margin-bottom: 10px; padding: 12px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); }.generation-panel header { display: flex; align-items: center; justify-content: space-between; }.generation-panel h4 { margin: 2px 0 0; font-size: 13px; }.generation-panel small { color: #2563eb; font-size: 8px; font-weight: 800; letter-spacing: .14em; }.generation-panel header span { padding: 3px 8px; border-radius: 999px; background: #dcfce7; color: #166534; font-size: 10px; }.generation-panel header span.running { background: #dbeafe; color: #1d4ed8; }.generation-panel ol { display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 8px; margin: 12px 0 0; padding: 0; list-style: none; }.generation-panel li { display: flex; gap: 8px; min-width: 0; padding: 8px; border-radius: 8px; background: var(--surface2, #f8fafc); opacity: .55; }.generation-panel li.success, .generation-panel li.running, .generation-panel li.warning { opacity: 1; }.generation-panel li > b { display: grid; width: 20px; height: 20px; flex: 0 0 20px; place-items: center; border-radius: 50%; background: #e2e8f0; font-size: 10px; }.generation-panel li.success > b { background: #dcfce7; color: #166534; }.generation-panel li.running > b { background: #dbeafe; color: #1d4ed8; }.generation-panel li.warning > b { background: #fef3c7; color: #92400e; }.generation-panel strong { display: block; font-size: 11px; }.generation-panel p, .generation-panel code { display: block; margin: 4px 0 0; overflow: hidden; color: var(--text-muted); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }@media (max-width: 650px) { .generation-panel ol { grid-template-columns: 1fr 1fr; } }
</style>
