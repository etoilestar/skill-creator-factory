<template>
  <section class="graph-builder" :class="{ archiving }" aria-live="polite">
    <header>
      <div><small>RESPONSIBILITY GRAPH · LIVE</small><h3>正在搭建责任与接口图谱</h3></div>
      <span class="live-pill"><i />{{ archiving ? '正在归档' : '构建中' }}</span>
    </header>

    <div class="build-track">
      <div v-for="(step, index) in steps" :key="step.label" :class="step.state">
        <b>{{ step.state === 'done' ? '✓' : index + 1 }}</b><span>{{ step.label }}</span><em>{{ step.hint }}</em>
      </div>
    </div>

    <div class="graph-canvas">
      <div class="node-column">
        <article v-for="(node, index) in visibleNodes" :key="node.target_file" class="node-card" :style="{ '--delay': `${index * 70}ms` }">
          <span class="node-index">0{{ index + 1 }}</span>
          <div><strong>{{ basename(node.target_file) }}</strong><p>{{ node.purpose || node.role || '正在确认职责边界' }}</p></div>
          <span class="contract-count">{{ (node.inputs || []).length }} IN · {{ (node.outputs || []).length }} OUT</span>
        </article>
        <p v-if="!visibleNodes.length" class="skeleton-copy"><i />正在拆分功能责任，首个节点即将出现…</p>
      </div>
      <div class="connection-feed">
        <strong>连接过程</strong>
        <p v-for="(edge, index) in visibleEdges" :key="`${edge.from_node}-${edge.to_node}-${index}`">
          <span>{{ basename(edge.from_node) }}</span><i>→</i><span>{{ basename(edge.to_node) }}</span>
          <em>{{ edge.contract || edge.label || edge.type || '数据 / 调用' }}</em>
        </p>
        <p v-if="!visibleEdges.length" class="muted">等待节点就绪后连接数据与调用合同</p>
      </div>
    </div>

    <footer><span class="pulse" />{{ currentOperation }}<b>{{ nodes.length }} 节点 · {{ edges.length }} 连接</b></footer>
  </section>
</template>
<script setup>
import { computed } from 'vue'
const props = defineProps({ events: { type: Array, default: () => [] }, nodes: { type: Array, default: () => [] }, edges: { type: Array, default: () => [] }, archiving: Boolean })
const graphEvents = computed(() => props.events.filter(event => event.stage === 'graph' || event.stage === 'repair'))
const latest = computed(() => graphEvents.value.at(-1))
const currentOperation = computed(() => props.archiving ? '图谱已确定，正在收进右侧执行过程' : (latest.value?.message || latest.value?.title || '分析蓝图中的职责边界'))
const visibleNodes = computed(() => props.nodes.slice(0, 8))
const visibleEdges = computed(() => props.edges.slice(0, 8))
const steps = computed(() => {
  const progress = Math.min(4, Math.max(graphEvents.value.length, props.nodes.length ? 2 : 0, props.edges.length ? 3 : 0))
  return [
    ['拆分职责', '从蓝图识别执行单元'], ['创建节点', '明确文件与单一责任'], ['连接合同', '匹配输入、输出和调用'], ['校验定稿', '消除断链并冻结结果'],
  ].map(([label, hint], index) => ({ label, hint, state: props.archiving || index < progress ? 'done' : index === progress ? 'active' : '' }))
})
const basename = path => String(path || '平台入口').split('/').pop() || '平台入口'
</script>
<style scoped>
.graph-builder{padding:20px;border:1px solid #bfdbfe;border-radius:18px;background:linear-gradient(145deg,#fff 0%,#f5f9ff 55%,#eef2ff 100%);box-shadow:0 16px 40px rgba(37,99,235,.1);overflow:hidden;transition:.65s cubic-bezier(.4,0,.2,1)}.graph-builder.archiving{transform:translateX(35%) scale(.86);opacity:0;filter:blur(3px)}header{display:flex;justify-content:space-between;align-items:start;gap:12px}h3{margin:4px 0 0;font-size:18px}small{color:#2563eb;font-weight:800;letter-spacing:.12em}.live-pill{padding:6px 10px;border-radius:999px;background:#dbeafe;color:#1d4ed8;font-size:11px;font-weight:700}.live-pill i,.pulse{display:inline-block;width:7px;height:7px;margin-right:7px;border-radius:50%;background:#3b82f6;animation:pulse 1.4s infinite}.build-track{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:18px 0}.build-track div{position:relative;padding:10px;border-radius:12px;background:#f1f5f9;color:#94a3b8}.build-track div.active{background:#eff6ff;color:#2563eb;box-shadow:inset 0 0 0 1px #93c5fd}.build-track div.done{background:#ecfdf5;color:#047857}.build-track b,.build-track span,.build-track em{display:block}.build-track b{float:left;width:22px;height:22px;margin-right:7px;border-radius:50%;background:#fff;text-align:center;line-height:22px}.build-track span{font-size:12px;font-weight:700}.build-track em{margin-top:4px;font-size:9px;font-style:normal}.graph-canvas{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(220px,.65fr);gap:12px}.node-column,.connection-feed{display:grid;align-content:start;gap:8px}.node-card{display:grid;grid-template-columns:32px 1fr auto;align-items:center;gap:10px;padding:11px;border:1px solid #dbeafe;border-radius:12px;background:rgba(255,255,255,.9);animation:arrive .4s both;animation-delay:var(--delay)}.node-index{color:#60a5fa;font:700 11px monospace}.node-card strong{font:700 12px monospace}.node-card p{margin:3px 0 0;color:var(--text-muted);font-size:10px}.contract-count{color:#4f46e5;font:700 9px monospace}.connection-feed{padding:12px;border-radius:12px;background:#172554;color:#dbeafe}.connection-feed>strong{font-size:11px;color:#93c5fd}.connection-feed p{display:grid;grid-template-columns:1fr auto 1fr;gap:5px;align-items:center;margin:0;padding:7px 0;border-bottom:1px solid rgba(147,197,253,.14);font:10px monospace}.connection-feed p i{color:#60a5fa}.connection-feed p em{grid-column:1/-1;color:#93c5fd;font-style:normal}.skeleton-copy{padding:20px;color:var(--text-muted);font-size:12px}.skeleton-copy i{display:inline-block;width:9px;height:9px;margin-right:8px;border-radius:50%;background:#93c5fd}footer{display:flex;align-items:center;margin-top:14px;color:#475569;font-size:11px}footer b{margin-left:auto;color:#1d4ed8}@keyframes arrive{from{opacity:0;transform:translateY(-8px)}}@keyframes pulse{50%{opacity:.35;box-shadow:0 0 0 7px rgba(59,130,246,.12)}}@media(max-width:720px){.build-track{grid-template-columns:1fr 1fr}.graph-canvas{grid-template-columns:1fr}.node-card{grid-template-columns:28px 1fr}.contract-count{grid-column:2}}
</style>
