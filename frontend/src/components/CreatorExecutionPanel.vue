<template>
  <div class="creator-execution-panel">
    <div class="execution-tabs" role="tablist">
      <button v-for="tab in tabs" :key="tab.key" type="button" :class="{ active: localActiveTab === tab.key }" @click="setTab(tab.key)">{{ tab.label }}</button>
    </div>
    <div class="execution-content">
      <section v-if="localActiveTab === 'process'" class="execution-tab-panel process-panel">
        <div v-if="currentStatus?.message" class="execution-current-status">
          <span class="status-spinner"></span>
          <span>{{ currentStatus.message }}</span>
        </div>
        <ThinkingPanel :thoughts="thoughts" content-only />
      </section>
      <section v-else-if="localActiveTab === 'graph'" class="execution-tab-panel graph-panel">
        <div class="graph-summary">
          <div><strong>{{ nodes.length }}</strong><span>功能节点</span></div>
          <div><strong>{{ inputCount }}</strong><span>输入合同</span></div>
          <div><strong>{{ outputCount }}</strong><span>输出 / 产物</span></div>
          <div><strong>{{ edges.length }}</strong><span>数据流 / 调用</span></div>
        </div>
        <div class="graph-legend"><span>● 功能节点</span><span>→ 数据流向与调用关系</span></div>
        <CreatorResponsibilityGraph :nodes="nodes" :edges="edges" />
      </section>
      <section v-else class="execution-tab-panel tools-panel">
        <div v-if="toolRows.length" class="creator-tool-grid">
          <div v-for="row in toolRows" :key="row.toolId || `${row.name}-${row.capability}`" class="creator-tool-card">
            <div class="tool-card-header">
              <strong>{{ row.name }}</strong>
              <span class="tool-status" :class="row.statusClass">{{ row.statusText }}</span>
            </div>
            <span v-if="row.capability" class="tool-capability">{{ row.capability }}</span>
            <div v-if="row.targetFiles?.length" class="tool-target-files">
              <span v-for="path in row.targetFiles" :key="path" class="tool-target-chip">{{ basename(path) }}</span>
            </div>
            <div v-if="row.matchedFeatures?.length" class="tool-feature-list">
              <span v-for="feature in row.matchedFeatures" :key="feature" class="tool-feature-chip">{{ feature }}</span>
            </div>
          </div>
        </div>
        <p v-else class="execution-empty">等待能力 / 工具匹配…</p>
        <div v-if="primaryToolBindings.length" class="binding-section">
          <h4>文件工具绑定</h4>
          <div class="binding-grid">
            <div v-for="binding in primaryToolBindings" :key="binding.targetFile" class="binding-card">
              <strong :title="binding.targetFile">{{ basename(binding.targetFile) }}</strong>
              <div class="binding-chips">
                <span v-for="toolId in binding.primaryToolIds" :key="toolId" class="binding-chip">{{ toolId }}</span>
              </div>
            </div>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, computed } from 'vue'
import ThinkingPanel from './ThinkingPanel.vue'
import CreatorResponsibilityGraph from './CreatorResponsibilityGraph.vue'

const props = defineProps({
  thoughts: { type: Array, default: () => [] },
  nodes: { type: Array, default: () => [] },
  edges: { type: Array, default: () => [] },
  toolRows: { type: Array, default: () => [] },
  primaryToolBindings: { type: Array, default: () => [] },
  streaming: { type: Boolean, default: false },
  currentStatus: { type: Object, default: null },
  activeTab: { type: String, default: 'process' },
})
const emit = defineEmits(['update:activeTab'])
const tabs = [{ key: 'process', label: '过程' }, { key: 'graph', label: '责任图谱' }, { key: 'tools', label: '工具' }]
const localActiveTab = ref(props.activeTab || 'process')
const inputCount = computed(() => props.nodes.reduce((count, node) => count + (Array.isArray(node.inputs) ? node.inputs.length : 0), 0))
const outputCount = computed(() => props.nodes.reduce((count, node) => count + (Array.isArray(node.outputs) ? node.outputs.length : 0), 0))
function setTab(tab) { localActiveTab.value = tab; emit('update:activeTab', tab) }
function basename(path) { return String(path || '').split('/').pop() || String(path || '') }
watch(() => props.activeTab, tab => { if (tab && tab !== localActiveTab.value) localActiveTab.value = tab })
</script>

<style scoped>
.creator-execution-panel { flex: 1; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
.execution-tabs { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; padding: 10px; border-bottom: 1px solid var(--border); background: var(--surface); }
.execution-tabs button { border: 1px solid var(--border); border-radius: 8px; padding: 7px 8px; background: var(--surface2, #f8fafc); color: var(--text); cursor: pointer; font-size: 13px; }
.execution-tabs button.active { background: #eff6ff; border-color: #93c5fd; color: #1d4ed8; font-weight: 700; }
.execution-content { flex: 1; min-height: 0; overflow: hidden; }
.execution-tab-panel { height: 100%; min-height: 0; overflow: auto; }
.process-panel { display: flex; flex-direction: column; }
.execution-current-status { display: flex; align-items: center; gap: 8px; margin: 10px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface2, #f8fafc); font-size: 13px; }
.status-spinner { width: 14px; height: 14px; border: 2px solid currentColor; border-top-color: transparent; border-radius: 50%; animation: spin .8s linear infinite; opacity: .7; }
.graph-panel { padding: 10px; overflow: hidden; }
.graph-summary { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin-bottom: 8px; }.graph-summary div { padding: 8px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface2, #f8fafc); }.graph-summary strong, .graph-summary span { display: block; }.graph-summary strong { color: #2563eb; font-size: 16px; }.graph-summary span { margin-top: 2px; color: var(--text-muted); font-size: 10px; }.graph-legend { display: flex; gap: 12px; margin-bottom: 8px; color: var(--text-muted); font-size: 11px; }
.tools-panel { padding: 12px; }
.creator-tool-grid, .binding-grid { display: grid; gap: 10px; }
.creator-tool-card, .binding-card { padding: 12px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface2, #f8fafc); }
.tool-card-header { display: flex; justify-content: space-between; gap: 8px; align-items: flex-start; }
.tool-card-header strong, .binding-card strong { font-family: 'Fira Code', 'Cascadia Code', monospace; word-break: break-all; }
.tool-status { padding: 2px 8px; border-radius: 999px; background: #e5e7eb; color: #374151; white-space: nowrap; font-size: 12px; }
.tool-status.ready { background: #dcfce7; color: #166534; }
.tool-status.blocked { background: #fee2e2; color: #991b1b; }
.tool-status.pending { background: #fef3c7; color: #92400e; }
.tool-capability { display: inline-block; margin-top: 8px; color: var(--text-muted); font-size: 12px; }
.tool-target-files, .tool-feature-list, .binding-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.tool-target-chip, .tool-feature-chip, .binding-chip { padding: 3px 7px; border-radius: 999px; background: #fff; border: 1px solid var(--border); font-size: 12px; font-family: monospace; }
.binding-section { margin-top: 18px; }
.binding-section h4 { margin: 0 0 10px; font-size: 13px; color: var(--text-muted); }
.execution-empty { margin: 24px 0; color: var(--text-muted); text-align: center; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
