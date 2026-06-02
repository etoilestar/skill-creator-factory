<template>
  <div class="task-plan-panel">
    <div v-if="!plan" class="plan-empty">
      <span>暂无执行方案</span>
    </div>

    <template v-else>
      <!-- Instruction Analysis Summary -->
      <div v-if="plan.instruction_analysis" class="analysis-section">
        <h4 class="section-title">📝 指令理解</h4>
        <div class="analysis-card">
          <div class="analysis-row">
            <span class="analysis-label">意图</span>
            <span class="analysis-value">{{ plan.instruction_analysis.intent }}</span>
          </div>
          <div class="analysis-row">
            <span class="analysis-label">范围</span>
            <span class="analysis-value">{{ plan.instruction_analysis.scope }}</span>
          </div>
          <div v-if="plan.instruction_analysis.constraints && plan.instruction_analysis.constraints.length" class="analysis-row">
            <span class="analysis-label">约束</span>
            <ul class="analysis-list">
              <li v-for="(c, i) in plan.instruction_analysis.constraints" :key="i">{{ c }}</li>
            </ul>
          </div>
          <div v-if="plan.instruction_analysis.output_requirements && plan.instruction_analysis.output_requirements.length" class="analysis-row">
            <span class="analysis-label">输出要求</span>
            <ul class="analysis-list">
              <li v-for="(r, i) in plan.instruction_analysis.output_requirements" :key="i">{{ r }}</li>
            </ul>
          </div>
          <div class="analysis-row">
            <span class="analysis-label">复杂度</span>
            <span class="complexity-badge" :class="plan.instruction_analysis.complexity">
              {{ complexityLabel(plan.instruction_analysis.complexity) }}
            </span>
          </div>
        </div>
      </div>

      <!-- Task Steps -->
      <div class="tasks-section">
        <h4 class="section-title">📋 执行步骤（共 {{ plan.total_tasks }} 步）</h4>
        <div class="task-list">
          <div
            v-for="(task, idx) in plan.tasks"
            :key="idx"
            class="task-item"
            :class="taskStatusClass(idx)"
          >
            <div class="task-number">{{ idx + 1 }}</div>
            <div class="task-content">
              <div class="task-action">
                <span class="task-action-icon">{{ actionIcon(task.action) }}</span>
                <span class="task-action-label">{{ actionLabel(task.action) }}</span>
              </div>
              <div v-if="task.command" class="task-command">{{ task.command }}</div>
              <div v-if="task.path" class="task-path">📁 {{ task.path }}</div>
              <div v-if="task.reason" class="task-reason">{{ task.reason }}</div>
            </div>
            <div class="task-status-icon">
              {{ statusIcon(idx) }}
            </div>
          </div>
        </div>
      </div>

      <!-- Confirmation Actions (Plan mode) -->
      <div v-if="plan.awaiting_confirmation" class="confirm-actions">
        <button class="btn-confirm" @click="$emit('confirm')" :disabled="confirming">
          {{ confirming ? '执行中…' : '✅ 确认执行' }}
        </button>
        <button class="btn-cancel" @click="$emit('cancel')" :disabled="confirming">
          ❌ 取消
        </button>
      </div>
    </template>
  </div>
</template>

<script setup>
defineProps({
  plan: {
    type: Object,
    default: null,
  },
  executingIndex: {
    type: Number,
    default: -1,
  },
  completedIndices: {
    type: Array,
    default: () => [],
  },
  confirming: {
    type: Boolean,
    default: false,
  },
})

defineEmits(['confirm', 'cancel'])

function complexityLabel(complexity) {
  const labels = { simple: '简单', moderate: '中等', complex: '复杂' }
  return labels[complexity] || complexity
}

function actionIcon(action) {
  const icons = {
    run_command: '⚡',
    read_resource: '📖',
    write_file: '✏️',
    create_directory: '📂',
    display: '👁️',
    ignore: '⏭️',
  }
  return icons[action] || '•'
}

function actionLabel(action) {
  const labels = {
    run_command: '执行命令',
    read_resource: '读取资源',
    write_file: '写入文件',
    create_directory: '创建目录',
    display: '展示',
    ignore: '忽略',
  }
  return labels[action] || action
}

function taskStatusClass(idx) {
  if (this?.completedIndices?.includes(idx)) return 'completed'
  if (this?.executingIndex === idx) return 'executing'
  return 'pending'
}

function statusIcon(idx) {
  if (this?.completedIndices?.includes(idx)) return '✅'
  if (this?.executingIndex === idx) return '⏳'
  return '⬜'
}
</script>

<style scoped>
.task-plan-panel {
  padding: 12px;
  font-size: 13px;
  overflow-y: auto;
  height: 100%;
}

.plan-empty {
  color: #999;
  text-align: center;
  padding: 24px 0;
  font-size: 12px;
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  margin: 0 0 8px 0;
  color: #333;
}

/* Instruction Analysis */
.analysis-section {
  margin-bottom: 16px;
}

.analysis-card {
  background: #f8f9fa;
  border-radius: 8px;
  padding: 10px 12px;
  border: 1px solid #e8e8e8;
}

.analysis-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 4px 0;
  border-bottom: 1px solid #f0f0f0;
}

.analysis-row:last-child {
  border-bottom: none;
}

.analysis-label {
  font-weight: 500;
  color: #666;
  min-width: 50px;
  font-size: 11px;
}

.analysis-value {
  color: #333;
  font-size: 12px;
  flex: 1;
}

.analysis-list {
  margin: 0;
  padding-left: 16px;
  font-size: 11px;
  color: #555;
}

.analysis-list li {
  margin: 2px 0;
}

.complexity-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 500;
}

.complexity-badge.simple { background: #d4edda; color: #155724; }
.complexity-badge.moderate { background: #fff3cd; color: #856404; }
.complexity-badge.complex { background: #f8d7da; color: #721c24; }

/* Task List */
.tasks-section {
  margin-bottom: 16px;
}

.task-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.task-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  background: #f8f9fa;
  border-radius: 8px;
  border: 1px solid #e8e8e8;
  transition: border-color 0.2s, background 0.2s;
}

.task-item.executing {
  border-color: #007bff;
  background: #e7f3ff;
}

.task-item.completed {
  border-color: #28a745;
  background: #e8f8ed;
}

.task-number {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #dee2e6;
  color: #495057;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  flex-shrink: 0;
}

.task-content {
  flex: 1;
  min-width: 0;
}

.task-action {
  display: flex;
  align-items: center;
  gap: 4px;
  font-weight: 500;
  font-size: 12px;
}

.task-action-icon { font-size: 13px; }
.task-action-label { color: #333; }

.task-command {
  font-family: monospace;
  font-size: 11px;
  color: #6c757d;
  background: #e9ecef;
  padding: 3px 6px;
  border-radius: 4px;
  margin-top: 4px;
  word-break: break-all;
  white-space: pre-wrap;
}

.task-path {
  font-size: 11px;
  color: #6c757d;
  margin-top: 3px;
}

.task-reason {
  font-size: 11px;
  color: #888;
  margin-top: 3px;
  font-style: italic;
}

.task-status-icon {
  flex-shrink: 0;
  font-size: 14px;
}

/* Confirmation Actions */
.confirm-actions {
  display: flex;
  gap: 8px;
  padding: 12px 0;
  border-top: 1px solid #e8e8e8;
  margin-top: 12px;
}

.btn-confirm {
  flex: 1;
  padding: 8px 16px;
  border: none;
  border-radius: 6px;
  background: #28a745;
  color: white;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.2s;
}

.btn-confirm:hover:not(:disabled) { background: #218838; }
.btn-confirm:disabled { opacity: 0.6; cursor: not-allowed; }

.btn-cancel {
  padding: 8px 16px;
  border: 1px solid #dc3545;
  border-radius: 6px;
  background: white;
  color: #dc3545;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.2s;
}

.btn-cancel:hover:not(:disabled) { background: #f8d7da; }
.btn-cancel:disabled { opacity: 0.6; cursor: not-allowed; }
</style>
