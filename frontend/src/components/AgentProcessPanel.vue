<template>
  <div class="agent-process" :class="`phase-${phase}`">
    <!-- Header -->
    <div class="process-header">
      <span class="process-icon">{{ phaseIcon }}</span>
      <span class="process-title">{{ phaseTitle }}</span>
      <span v-if="phase === 'executing'" class="process-spinner"></span>
    </div>

    <!-- Task list overview -->
    <div v-if="tasks.length" class="task-overview">
      <div
        v-for="(task, idx) in tasks"
        :key="task.task_id"
        class="task-row"
        :class="{ clickable: task.steps.length > 0 }"
        @click="toggleTask(idx)"
      >
        <span class="task-connector">
          <span class="connector-dot" :class="task.status"></span>
          <span v-if="idx < tasks.length - 1" class="connector-line"></span>
        </span>
        <span class="task-type-icon">{{ typeIcon(task.sub_agent_type) }}</span>
        <span class="task-type-badge" :class="`type-${task.sub_agent_type}`">
          {{ task.sub_agent_type }}
        </span>
        <span class="task-desc">{{ task.description }}</span>
        <span class="task-status-icon">{{ statusIcon(task.status) }}</span>
        <span v-if="task.steps.length" class="task-expand">{{ expandedTasks[idx] ? '▲' : '▼' }}</span>
      </div>
    </div>

    <!-- Expanded task detail -->
    <div v-for="(task, idx) in tasks" :key="`detail-${task.task_id}`">
      <div v-if="expandedTasks[idx] && task.steps.length" class="task-detail">
        <div class="detail-header">
          <span class="detail-type-icon">{{ typeIcon(task.sub_agent_type) }}</span>
          <span class="detail-title">{{ task.description }}</span>
        </div>
        <div v-for="(step, si) in task.steps" :key="si" class="step-item">
          <span class="step-icon">{{ step.success !== false ? '✅' : '❌' }}</span>
          <div class="step-content">
            <span class="step-action">{{ step.action }}</span>
            <span v-if="step.command" class="step-command">{{ step.command }}</span>
            <span v-if="step.path" class="step-path">{{ step.path }}</span>
            <span v-if="step.duration_ms" class="step-duration">({{ (step.duration_ms / 1000).toFixed(1) }}s)</span>
            <div v-if="step.output_files && step.output_files.length" class="step-files">
              <span class="step-files-label">📄</span>
              <span v-for="f in step.output_files" :key="f.path" class="step-file">{{ f.path }}</span>
            </div>
          </div>
        </div>
        <div v-if="task.error" class="task-error">
          <span class="error-icon">⚠️</span>
          <span class="error-text">{{ task.error }}</span>
        </div>
        <div v-if="task.replan_reason" class="task-replan">
          <span class="replan-icon">🔄</span>
          <span class="replan-text">{{ task.replan_reason }}</span>
        </div>
      </div>
    </div>

    <!-- ask_user card -->
    <div v-if="askUser && !askUser.answered" class="ask-user-card">
      <div class="ask-user-icon">❓</div>
      <div class="ask-user-message">{{ askUser.message }}</div>
      <div class="ask-user-input-row">
        <textarea
          v-model="askUserInput"
          rows="2"
          class="ask-user-textarea"
          placeholder="请输入补充信息…"
          @keydown.enter.exact.prevent="submitAskUser"
        ></textarea>
        <button class="ask-user-submit" :disabled="!askUserInput.trim()" @click="submitAskUser">
          发送
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'

const props = defineProps({
  /** The agent process data object */
  process: {
    type: Object,
    default: () => null,
  },
})

const emit = defineEmits(['ask-user-submit'])

const expandedTasks = ref({})
const askUserInput = ref('')

const tasks = computed(() => props.process?.tasks || [])
const phase = computed(() => props.process?.phase || 'decomposing')
const askUser = computed(() => props.process?.askUser || null)

const phaseIcon = computed(() => {
  switch (phase.value) {
    case 'decomposing': return '🧠'
    case 'executing': return '🤖'
    case 'completed': return '✅'
    case 'ask_user': return '❓'
    default: return '🤖'
  }
})

const phaseTitle = computed(() => {
  switch (phase.value) {
    case 'decomposing': return '正在分析任务…'
    case 'executing': return '正在执行…'
    case 'completed': return '执行完成'
    case 'ask_user': return '需要补充信息'
    default: return '正在执行…'
  }
})

const TYPE_ICONS = {
  data_query: '🔍',
  code_script: '⚙️',
  network_api: '🌐',
  document: '📄',
  validation: '✅',
}

function typeIcon(type) {
  return TYPE_ICONS[type] || '•'
}

function statusIcon(status) {
  switch (status) {
    case 'pending': return '⬜'
    case 'running': return '🔵'
    case 'completed': return '🟢'
    case 'failed': return '🔴'
    default: return '⬜'
  }
}

function toggleTask(idx) {
  expandedTasks.value[idx] = !expandedTasks.value[idx]
}

function submitAskUser() {
  const text = askUserInput.value.trim()
  if (!text) return
  emit('ask-user-submit', text)
  askUserInput.value = ''
}
</script>

<style scoped>
.agent-process {
  border-radius: 10px;
  border: 1px solid var(--border, #e0e0e0);
  background: var(--surface, #fafafa);
  overflow: hidden;
  font-size: 13px;
  animation: fade-in 0.2s ease;
}

.agent-process.phase-decomposing { border-color: var(--color-yellow-border); }
.agent-process.phase-executing { border-color: var(--color-blue-border); }
.agent-process.phase-completed { border-color: var(--color-green-border); }
.agent-process.phase-ask_user { border-color: var(--color-red-border); }

/* Header */
.process-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  background: rgba(255, 255, 255, 0.03);
  border-bottom: 1px solid var(--border, #2e3348);
  font-weight: 600;
}

.process-icon { font-size: 16px; }
.process-title { flex: 1; }

.process-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--color-blue-text);
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

/* Task overview */
.task-overview {
  padding: 8px 0;
}

.task-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 14px 5px 8px;
  transition: background 0.15s;
  min-height: 28px;
}

.task-row.clickable {
  cursor: pointer;
}

.task-row.clickable:hover {
  background: rgba(255, 255, 255, 0.04);
}

/* Connector dot + line */
.task-connector {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 16px;
  flex-shrink: 0;
  align-self: stretch;
}

.connector-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
  margin-top: 4px;
}

.connector-dot.pending { background: #6b7280; }
.connector-dot.running { background: var(--color-blue-text); animation: pulse-dot 1.5s ease infinite; }
.connector-dot.completed { background: var(--color-green-text); }
.connector-dot.failed { background: var(--color-red-text); }

.connector-line {
  width: 2px;
  flex: 1;
  background: var(--border, #2e3348);
  min-height: 4px;
}

.task-type-icon { font-size: 14px; flex-shrink: 0; }

.task-type-badge {
  font-size: 10px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 4px;
  white-space: nowrap;
  flex-shrink: 0;
  font-family: monospace;
}

.type-data_query { background: var(--color-blue-bg); color: var(--color-blue-text); }
.type-code_script { background: var(--color-yellow-bg); color: var(--color-yellow-text); }
.type-network_api { background: var(--color-purple-bg); color: var(--color-purple-text); }
.type-document { background: var(--color-green-bg); color: var(--color-green-text); }
.type-validation { background: var(--color-pink-bg); color: var(--color-pink-text); }

.task-desc {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text, #e2e5f0);
}

.task-status-icon { font-size: 12px; flex-shrink: 0; }
.task-expand { font-size: 9px; color: var(--text-muted, #9ca3b8); flex-shrink: 0; }

/* Task detail */
.task-detail {
  margin: 0 14px 8px 24px;
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.06);
  animation: fade-in 0.2s ease;
}

.detail-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
  font-weight: 600;
  font-size: 12px;
  color: var(--text-muted, #9ca3b8);
}

.detail-type-icon { font-size: 13px; }
.detail-title { flex: 1; }

.step-item {
  display: flex;
  gap: 6px;
  padding: 3px 0;
  font-size: 12px;
}

.step-icon { font-size: 12px; flex-shrink: 0; margin-top: 1px; }

.step-content {
  flex: 1;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 8px;
}

.step-action {
  font-weight: 600;
  color: var(--text-muted, #9ca3b8);
  font-family: monospace;
  font-size: 11px;
}

.step-command, .step-path {
  font-family: 'Fira Code', 'Cascadia Code', monospace;
  font-size: 11px;
  color: var(--text-muted, #9ca3b8);
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 5px;
  border-radius: 3px;
  word-break: break-all;
}

.step-duration {
  font-size: 11px;
  color: var(--text-muted, #9ca3b8);
}

.step-files {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  width: 100%;
}

.step-files-label { font-size: 12px; }

.step-file {
  font-family: monospace;
  font-size: 11px;
  color: var(--color-blue-text);
  background: var(--color-blue-bg);
  padding: 1px 5px;
  border-radius: 3px;
}

.task-error {
  display: flex;
  gap: 6px;
  margin-top: 6px;
  padding: 4px 8px;
  border-radius: 4px;
  background: var(--color-red-bg);
  color: var(--color-red-text);
  font-size: 12px;
}

.error-icon { flex-shrink: 0; }
.error-text { flex: 1; word-break: break-all; }

.task-replan {
  display: flex;
  gap: 6px;
  margin-top: 4px;
  padding: 4px 8px;
  border-radius: 4px;
  background: var(--color-yellow-bg);
  color: var(--color-yellow-text);
  font-size: 12px;
}

.replan-icon { flex-shrink: 0; }
.replan-text { flex: 1; word-break: break-all; }

/* ask_user card */
.ask-user-card {
  margin: 8px 14px 10px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--color-red-bg);
  border: 1px solid var(--color-red-border);
}

.ask-user-icon { font-size: 16px; margin-bottom: 6px; }

.ask-user-message {
  font-size: 13px;
  color: var(--color-red-text);
  margin-bottom: 8px;
  white-space: pre-wrap;
}

.ask-user-input-row {
  display: flex;
  gap: 8px;
  align-items: flex-end;
}

.ask-user-textarea {
  flex: 1;
  min-height: 40px;
  padding: 6px 8px;
  border-radius: 6px;
  border: 1px solid var(--border, #2e3348);
  font-size: 13px;
  resize: vertical;
  font-family: inherit;
}

.ask-user-textarea:focus {
  outline: none;
  border-color: var(--accent, #6c8aff);
  box-shadow: 0 0 0 2px rgba(108, 138, 255, 0.3);
}

.ask-user-submit {
  padding: 6px 14px;
  border-radius: 6px;
  border: none;
  background: var(--accent, #6c8aff);
  color: white;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s;
  white-space: nowrap;
}

.ask-user-submit:hover:not(:disabled) { background: var(--accent-hover, #8aa4ff); }
.ask-user-submit:disabled { opacity: 0.5; cursor: not-allowed; }

/* Animations */
@keyframes spin {
  to { transform: rotate(360deg); }
}

@keyframes pulse-dot {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

@keyframes fade-in {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}
</style>
