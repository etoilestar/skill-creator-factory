<template>
  <div class="sop-panel">
    <div v-if="!sop" class="sop-empty">
      <span>暂无 SOP 方案</span>
    </div>

    <template v-else>
      <div class="sop-header">
        <h4 class="sop-title">{{ sop.title }}</h4>
        <div class="sop-meta">
          <span class="meta-item">版本 {{ sop.version }}</span>
          <span class="meta-item">{{ sop.total_steps }} 步骤</span>
          <span v-if="sop.complexity" class="meta-item complexity" :class="sop.complexity">
            {{ complexityLabel(sop.complexity) }}
          </span>
        </div>
      </div>

      <!-- View Toggle -->
      <div class="view-toggle">
        <button
          class="toggle-btn"
          :class="{ active: view === 'steps' }"
          @click="view = 'steps'"
        >步骤视图</button>
        <button
          class="toggle-btn"
          :class="{ active: view === 'flow' }"
          @click="view = 'flow'"
        >流程图</button>
      </div>

      <!-- Steps View -->
      <div v-if="view === 'steps'" class="steps-view">
        <div
          v-for="step in sop.steps"
          :key="step.order"
          class="step-card"
        >
          <div class="step-order">{{ step.order }}</div>
          <div class="step-body">
            <div class="step-name">{{ step.name }}</div>
            <div v-if="step.description" class="step-desc">{{ step.description }}</div>
            <div class="step-meta-row">
              <span v-if="step.inputs && step.inputs.length" class="step-io">
                📥 {{ step.inputs.join(', ') }}
              </span>
              <span v-if="step.outputs && step.outputs.length" class="step-io">
                📤 {{ step.outputs.join(', ') }}
              </span>
              <span class="step-responsible">👤 {{ step.responsible }}</span>
            </div>
          </div>
          <!-- Arrow connector -->
          <div v-if="step.order < sop.total_steps" class="step-arrow">↓</div>
        </div>
      </div>

      <!-- Flow View (Mermaid source) -->
      <div v-if="view === 'flow'" class="flow-view">
        <pre class="mermaid-source">{{ sop.flowchart_mermaid }}</pre>
        <p class="flow-hint">💡 复制上方 Mermaid 代码可在支持 Mermaid 的工具中渲染流程图</p>
      </div>

      <!-- Export Button -->
      <div class="export-actions">
        <button class="btn-export" @click="$emit('export', 'markdown')">
          📄 导出 Markdown
        </button>
        <button class="btn-export" @click="$emit('export', 'json')">
          📋 导出 JSON
        </button>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref } from 'vue'

defineProps({
  sop: {
    type: Object,
    default: null,
  },
})

defineEmits(['export'])

const view = ref('steps')

function complexityLabel(complexity) {
  const labels = { simple: '简单', moderate: '中等', complex: '复杂' }
  return labels[complexity] || complexity
}
</script>

<style scoped>
.sop-panel {
  padding: 12px;
  font-size: 13px;
  overflow-y: auto;
  height: 100%;
}

.sop-empty {
  color: #999;
  text-align: center;
  padding: 24px 0;
  font-size: 12px;
}

.sop-header {
  margin-bottom: 12px;
}

.sop-title {
  font-size: 14px;
  font-weight: 600;
  margin: 0 0 6px 0;
  color: #222;
}

.sop-meta {
  display: flex;
  gap: 10px;
  font-size: 11px;
  color: #666;
}

.meta-item {
  background: #f0f0f0;
  padding: 2px 8px;
  border-radius: 10px;
}

.meta-item.complexity.simple { background: #d4edda; color: #155724; }
.meta-item.complexity.moderate { background: #fff3cd; color: #856404; }
.meta-item.complexity.complex { background: #f8d7da; color: #721c24; }

/* View Toggle */
.view-toggle {
  display: flex;
  gap: 4px;
  margin-bottom: 12px;
  background: #f0f0f0;
  padding: 3px;
  border-radius: 6px;
}

.toggle-btn {
  flex: 1;
  padding: 5px 10px;
  border: none;
  background: transparent;
  border-radius: 4px;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.15s;
}

.toggle-btn.active {
  background: white;
  box-shadow: 0 1px 3px rgba(0,0,0,0.1);
  font-weight: 500;
}

/* Steps View */
.steps-view {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.step-card {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px;
  background: #fafbfc;
  border: 1px solid #e8e8e8;
  border-radius: 8px;
}

.step-order {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: #007bff;
  color: white;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 600;
  flex-shrink: 0;
}

.step-body {
  flex: 1;
  min-width: 0;
}

.step-name {
  font-weight: 500;
  color: #333;
  font-size: 12px;
  margin-bottom: 3px;
}

.step-desc {
  font-size: 11px;
  color: #666;
  margin-bottom: 4px;
  word-break: break-all;
  white-space: pre-wrap;
}

.step-meta-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 10px;
  color: #888;
}

.step-io {
  background: #f0f0f0;
  padding: 1px 6px;
  border-radius: 4px;
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.step-arrow {
  position: absolute;
  bottom: -14px;
  left: 22px;
  color: #ccc;
  font-size: 14px;
  z-index: 1;
}

/* Flow View */
.flow-view {
  padding: 8px;
}

.mermaid-source {
  background: #1e1e2e;
  color: #cdd6f4;
  padding: 12px;
  border-radius: 8px;
  font-size: 11px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

.flow-hint {
  font-size: 11px;
  color: #999;
  margin-top: 8px;
  font-style: italic;
}

/* Export */
.export-actions {
  display: flex;
  gap: 8px;
  padding: 12px 0;
  border-top: 1px solid #e8e8e8;
  margin-top: 12px;
}

.btn-export {
  padding: 6px 12px;
  border: 1px solid #dee2e6;
  border-radius: 6px;
  background: white;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.15s;
}

.btn-export:hover {
  background: #f0f2f5;
}
</style>
