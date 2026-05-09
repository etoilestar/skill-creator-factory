<template>
  <div class="thinking-panel">
    <div v-if="thoughts.length === 0" class="thinking-empty">
      <span>暂无执行记录</span>
    </div>

    <div v-else class="thinking-timeline">
      <div
        v-for="(thought, idx) in thoughts"
        :key="idx"
        class="thought-item"
        :class="[`step-${thought.step}`, { expanded: expanded[idx] }]"
      >
        <!-- Connector line (skip for first item) -->
        <div v-if="idx > 0" class="connector" />

        <div class="thought-header" @click="toggle(idx)">
          <span class="thought-icon">{{ stepIcon(thought.step, thought.data) }}</span>
          <div class="thought-text">
            <span class="thought-label">{{ thought.label }}</span>
            <span class="thought-detail">{{ thought.detail }}</span>
          </div>
          <span class="thought-toggle">{{ expanded[idx] ? '▲' : '▼' }}</span>
        </div>

        <div v-if="expanded[idx]" class="thought-body">
          <pre class="thought-data">{{ JSON.stringify(thought.data, null, 2) }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'

const props = defineProps({
  thoughts: {
    type: Array,
    default: () => [],
  },
})

// Track which items are expanded; keyed by index.
const expanded = ref({})

function toggle(idx) {
  expanded.value[idx] = !expanded.value[idx]
}

// Reset expansion state when thoughts list is replaced (new round).
watch(
  () => props.thoughts,
  (newVal, oldVal) => {
    if (newVal !== oldVal && newVal.length === 0) {
      expanded.value = {}
    }
  },
)

const STEP_ICONS = {
  metadata_decision: '🔍',
  body_loaded: '📖',
  child_decision: '🧩',
  resource_selection: '📦',
  planner_output: '📋',
  action_start: '▶️',
  action_result: null, // dynamic — see stepIcon()
  final_answer: '💬',
}

function stepIcon(step, data) {
  if (step === 'action_result') {
    const success = data?.success ?? data?.success !== false
    return success ? '✅' : '❌'
  }
  return STEP_ICONS[step] ?? '•'
}
</script>

<style scoped>
.thinking-panel {
  display: flex;
  flex-direction: column;
  gap: 0;
  padding: 12px 8px;
  font-size: 13px;
  overflow-y: auto;
  height: 100%;
}

.thinking-empty {
  color: #999;
  text-align: center;
  padding: 24px 0;
  font-size: 12px;
}

.thinking-timeline {
  display: flex;
  flex-direction: column;
  position: relative;
}

.thought-item {
  position: relative;
  padding-left: 8px;
}

/* Vertical connecting line between thought items */
.connector {
  position: absolute;
  top: -8px;
  left: 16px;
  width: 2px;
  height: 10px;
  background: #e0e0e0;
}

.thought-header {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 6px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s;
  background: #f8f9fa;
  border-left: 3px solid transparent;
  margin-bottom: 2px;
}

.thought-header:hover {
  background: #f0f2f5;
}

/* Step-specific accent colours */
.step-metadata_decision .thought-header { border-left-color: #8ba3be; }
.step-body_loaded       .thought-header { border-left-color: #4a90d9; }
.step-child_decision    .thought-header { border-left-color: #9b59b6; }
.step-resource_selection .thought-header { border-left-color: #e67e22; }
.step-planner_output    .thought-header { border-left-color: #27ae60; }
.step-action_start      .thought-header { border-left-color: #1abc9c; }
.step-action_result     .thought-header { border-left-color: #2ecc71; }
.step-action_result.expanded .thought-header { border-left-color: #e74c3c; }
.step-final_answer      .thought-header { border-left-color: #95a5a6; }

.thought-icon {
  font-size: 15px;
  line-height: 1;
  flex-shrink: 0;
  margin-top: 1px;
}

.thought-text {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.thought-label {
  font-weight: 600;
  color: #333;
  font-size: 12px;
}

.thought-detail {
  color: #666;
  font-size: 11px;
  word-break: break-all;
  white-space: pre-wrap;
}

.thought-toggle {
  font-size: 10px;
  color: #aaa;
  flex-shrink: 0;
  align-self: center;
}

.thought-body {
  margin: 0 0 6px 8px;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid #e8e8e8;
}

.thought-data {
  background: #1e1e2e;
  color: #cdd6f4;
  margin: 0;
  padding: 10px 12px;
  font-size: 11px;
  line-height: 1.5;
  overflow-x: auto;
  max-height: 300px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
