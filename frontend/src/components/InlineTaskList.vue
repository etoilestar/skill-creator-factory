<template>
  <div class="inline-task-list">
    <div v-if="!tasks || !tasks.length" class="task-empty muted">暂无任务</div>
    <div v-else>
      <div
        v-for="(task, idx) in tasks"
        :key="idx"
        class="task-item"
        :class="taskStatusClass(idx)"
      >
        <span class="task-check">{{ taskCheckIcon(idx) }}</span>
        <span class="task-desc" :class="{ completed: isCompleted(idx) }">
          <span class="task-action-badge" :class="`badge-${task.action || 'default'}`">
            {{ actionLabel(task.action) }}
          </span>
          <span class="task-text">{{ taskDescription(task) }}</span>
        </span>
      </div>
    </div>
  </div>
</template>

<script setup>
const ACTION_LABELS = {
  run_command: '命令',
  write_file: '写入',
  read_resource: '读取',
  create_directory: '目录',
  display: '展示',
  ignore: '忽略',
}

function actionLabel(action) {
  return ACTION_LABELS[action] || action || '任务'
}

const props = defineProps({
  tasks: { type: Array, default: () => [] },
  completedIndices: { type: Array, default: () => [] },
  executingIndex: { type: Number, default: -1 },
})

function isCompleted(idx) {
  return props.completedIndices.includes(idx)
}

function isExecuting(idx) {
  return props.executingIndex === idx
}

function taskStatusClass(idx) {
  if (isCompleted(idx)) return 'status-completed'
  if (isExecuting(idx)) return 'status-executing'
  return 'status-pending'
}

function taskCheckIcon(idx) {
  if (isCompleted(idx)) return '\u2705'
  if (isExecuting(idx)) return '\u23F3'
  return '\u2B1C'
}

function taskDescription(task) {
  const action = task.action || ''
  if (action === 'run_command') {
    const cmd = task.command || ''
    return cmd.length > 60 ? cmd.slice(0, 60) + '...' : cmd
  }
  if (action === 'write_file' || action === 'create_directory') {
    return task.path || task.description || ''
  }
  if (action === 'read_resource') {
    return task.path || task.description || ''
  }
  return task.description || ''
}
</script>

<style scoped>
.inline-task-list {
  padding: 4px 0;
}

.task-empty {
  padding: 8px;
  font-size: 0.85em;
}

.task-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 4px 8px;
  border-radius: 4px;
  margin: 2px 0;
  transition: background-color 0.2s, opacity 0.2s;
}

.task-item.status-pending {
  background: transparent;
}

.task-item.status-executing {
  background: #e7f3ff;
  border-left: 3px solid #2196f3;
}

.task-item.status-completed {
  background: #e8f8ed;
  border-left: 3px solid #4caf50;
}

.task-check {
  flex-shrink: 0;
  font-size: 0.9em;
  line-height: 1.6;
}

.task-desc {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
  font-size: 0.9em;
  line-height: 1.6;
}

.task-desc.completed {
  text-decoration: line-through;
  color: var(--text-muted, #999);
  opacity: 0.7;
}

.task-action-badge {
  flex-shrink: 0;
  display: inline-block;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 0.75em;
  font-weight: 600;
  text-transform: uppercase;
}

.badge-run_command { background: #e3f2fd; color: #1565c0; }
.badge-write_file { background: #fff3e0; color: #e65100; }
.badge-read_resource { background: #e8f5e9; color: #2e7d32; }
.badge-create_directory { background: #f3e5f5; color: #6a1b9a; }
.badge-display { background: #f5f5f5; color: #616161; }
.badge-ignore { background: #f5f5f5; color: #9e9e9e; }
.badge-default { background: #f5f5f5; color: #757575; }

.task-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font, monospace);
  font-size: 0.85em;
}
</style>
