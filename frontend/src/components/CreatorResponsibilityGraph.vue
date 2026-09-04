<template>
  <div class="creator-responsibility-graph-wrap">
    <div v-if="!hasGraph" class="graph-empty">当前 Skill 没有可执行脚本责任图谱</div>
    <template v-else>
      <div class="creator-responsibility-graph">
        <button class="fit-view-button" type="button" title="将全部节点适配到窗口" @click="fitGraph">适配窗口</button>
        <VueFlow
          :nodes="flowNodes"
          :edges="flowEdges"
          :nodes-draggable="true"
          :nodes-connectable="false"
          :elements-selectable="true"
          :delete-key-code="null"
          :zoom-on-scroll="true"
          :pan-on-drag="true"
          :fit-view-on-init="true"
          @node-click="handleNodeClick"
        >
          <template #node-responsibility="nodeProps"><CreatorResponsibilityNode v-bind="nodeProps" /></template>
          <template #node-platformInput="nodeProps"><CreatorPlatformBoundaryNode v-bind="nodeProps" /></template>
          <template #node-platformOutput="nodeProps"><CreatorPlatformBoundaryNode v-bind="nodeProps" /></template>
          <Background />
          <Controls />
          <MiniMap pannable zoomable />
        </VueFlow>
      </div>
      <aside v-if="selectedNode" class="node-detail-drawer">
        <button class="detail-close" type="button" @click="selectedNode = null">✕</button>
        <h4>节点详情</h4>
        <p><strong>完整路径：</strong><code>{{ selectedNode.id }}</code></p>
        <p v-if="selectedNode.data?.role"><strong>role：</strong>{{ selectedNode.data.role }}</p>
        <p v-if="selectedNode.data?.purpose"><strong>purpose：</strong>{{ selectedNode.data.purpose }}</p>
        <DetailChips title="inputs" :items="selectedNode.data?.inputs" />
        <DetailChips title="outputs" :items="selectedNode.data?.outputs" />
        <DetailChips title="required capabilities" :items="selectedNode.data?.requiredCapabilities" />
        <DetailEdges title="incoming edges" :edges="incomingEdges" />
        <DetailEdges title="outgoing edges" :edges="outgoingEdges" />
      </aside>
    </template>
  </div>
</template>

<script setup>
import { computed, defineComponent, h, nextTick, ref, watch } from 'vue'
import { VueFlow, useVueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import dagre from '@dagrejs/dagre'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
import '@vue-flow/minimap/dist/style.css'
import CreatorResponsibilityNode from './CreatorResponsibilityNode.vue'
import CreatorPlatformBoundaryNode from './CreatorPlatformBoundaryNode.vue'


const props = defineProps({
  nodes: { type: Array, default: () => [] },
  edges: { type: Array, default: () => [] },
})

const DetailChips = defineComponent({
  props: { title: String, items: Array },
  setup(p) { return () => p.items?.length ? h('div', { class: 'detail-section' }, [h('strong', p.title), h('div', { class: 'detail-chips' }, p.items.map(item => h('span', { class: 'detail-chip', key: item }, item)))]) : null },
})
const DetailEdges = defineComponent({
  props: { title: String, edges: Array },
  setup(p) { return () => p.edges?.length ? h('div', { class: 'detail-section' }, [h('strong', p.title), ...p.edges.map((edge, i) => h('div', { class: 'detail-edge', key: i }, `${edge.from_node || ''}.${edge.from_output || ''} → ${edge.to_node || ''}.${edge.to_input || ''}`))]) : null },
})

const flowNodes = ref([])
const flowEdges = ref([])
const selectedNode = ref(null)
const { fitView } = useVueFlow()
const hasGraph = computed(() => props.nodes.length > 0 || props.edges.length > 0)
const incomingEdges = computed(() => props.edges.filter(edge => edge.to_node === selectedNode.value?.id))
const outgoingEdges = computed(() => props.edges.filter(edge => edge.from_node === selectedNode.value?.id))

function basename(path) { return String(path || '').split('/').pop() || String(path || '') }
function list(value) { return Array.isArray(value) ? value.map(item => String(item || '').trim()).filter(Boolean) : [] }
function responsibilityEdgeLabel(edge) {
  const fromOutput = String(edge.from_output || '').trim()
  const toInput = String(edge.to_input || '').trim()
  if (fromOutput && toInput && fromOutput !== toInput) return `${fromOutput} → ${toInput}`
  return fromOutput || toInput || ''
}
function mapNodes() {
  const mapped = props.nodes.map(item => ({
    id: String(item.target_file || '').trim(), type: 'responsibility', position: { x: 0, y: 0 },
    data: { targetFile: String(item.target_file || '').trim(), fileName: basename(item.target_file), role: String(item.role || '').trim(), purpose: String(item.purpose || '').trim(), inputs: list(item.inputs), outputs: list(item.outputs), requiredCapabilities: list(item.required_capabilities) },
  })).filter(node => node.id)
  const ids = new Set(mapped.map(node => node.id))
  for (const edge of props.edges) {
    if (edge.from_node === 'platform_input_node' && !ids.has('platform_input_node')) {
      mapped.push({ id: 'platform_input_node', type: 'platformInput', position: { x: 0, y: 0 }, data: { label: '平台输入', nodeId: 'platform_input_node', kind: 'input' } })
      ids.add('platform_input_node')
    }
    if (edge.to_node === 'platform_output_node' && !ids.has('platform_output_node')) {
      mapped.push({ id: 'platform_output_node', type: 'platformOutput', position: { x: 0, y: 0 }, data: { label: '平台输出', nodeId: 'platform_output_node', kind: 'output' } })
      ids.add('platform_output_node')
    }
  }
  return mapped
}
function mapEdges() {
  return props.edges.map((edge, index) => ({ id: `responsibility-edge-${index}`, source: String(edge.from_node || '').trim(), target: String(edge.to_node || '').trim(), type: 'smoothstep', markerEnd: 'arrowclosed', label: responsibilityEdgeLabel(edge), data: { fromOutput: edge.from_output || '', toInput: edge.to_input || '', purpose: edge.purpose || '', constraints: Array.isArray(edge.constraints) ? edge.constraints : [] } })).filter(edge => edge.source && edge.target)
}
function layoutGraph(nodes, edges) {
  const graph = new dagre.graphlib.Graph()
  graph.setDefaultEdgeLabel(() => ({}))
  graph.setGraph({ rankdir: 'TB', ranksep: 110, nodesep: 60, marginx: 24, marginy: 24 })
  const nodeWidth = 250; const nodeHeight = 210
  nodes.forEach(node => graph.setNode(node.id, { width: nodeWidth, height: nodeHeight }))
  edges.forEach(edge => graph.setEdge(edge.source, edge.target))
  dagre.layout(graph)
  return nodes.map(node => { const position = graph.node(node.id) || { x: 0, y: 0 }; return { ...node, position: { x: position.x - nodeWidth / 2, y: position.y - nodeHeight / 2 } } })
}
async function refreshGraph() {
  if (!hasGraph.value) { flowNodes.value = []; flowEdges.value = []; selectedNode.value = null; return }
  const edges = mapEdges(); flowEdges.value = edges; flowNodes.value = layoutGraph(mapNodes(), edges)
  await nextTick(); fitView({ padding: 0.18, duration: 250 })
}
function handleNodeClick(event) { selectedNode.value = event.node }
function fitGraph() { fitView({ padding: 0.22, duration: 250 }) }
watch(() => [props.nodes, props.edges], refreshGraph, { deep: true, immediate: true })
</script>

<style scoped>
.creator-responsibility-graph-wrap { position: relative; width: 100%; height: 100%; min-height: 520px; display: flex; flex-direction: column; overflow: hidden; }
.creator-responsibility-graph { position: relative; width: 100%; height: 100%; min-height: 520px; border: 1px solid var(--border); border-radius: 12px; overflow: hidden; background: #f8fafc; contain: layout paint; }
.creator-responsibility-graph :deep(.vue-flow), .creator-responsibility-graph :deep(.vue-flow__viewport), .creator-responsibility-graph :deep(svg) { width: 100%; height: 100%; }
.creator-responsibility-graph :deep(.vue-flow__edge-textbg) { fill: #fff; fill-opacity: .9; }.creator-responsibility-graph :deep(.vue-flow__edge-text) { font-size: 10px; }
.fit-view-button { position: absolute; z-index: 6; top: 10px; right: 10px; padding: 6px 9px; border: 1px solid #cbd5e1; border-radius: 7px; background: rgb(255 255 255 / 92%); color: #334155; cursor: pointer; font-size: 11px; box-shadow: 0 2px 8px rgb(15 23 42 / 10%); }
.graph-empty { margin: auto; padding: 24px; color: var(--text-muted); text-align: center; }
.node-detail-drawer { position: absolute; right: 14px; bottom: 14px; width: min(340px, calc(100% - 28px)); max-height: 48%; overflow: auto; padding: 14px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface, #fff); box-shadow: 0 18px 50px rgba(15,23,42,.18); font-size: 12px; z-index: 5; }
.node-detail-drawer h4 { margin: 0 28px 10px 0; }
.detail-close { position: absolute; top: 8px; right: 8px; border: 0; background: transparent; cursor: pointer; }
.detail-section { margin-top: 10px; }
.detail-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 5px; }
.detail-chip { padding: 2px 7px; border-radius: 999px; background: var(--surface2, #f1f5f9); font-family: monospace; }
.detail-edge { margin-top: 5px; padding: 6px; border-radius: 8px; background: var(--surface2, #f8fafc); word-break: break-all; font-family: monospace; }
</style>
