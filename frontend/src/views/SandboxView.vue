<template>
  <div class="sandbox">
    <div class="header">
      <h2>沙盒测试</h2>
      <p class="muted">勾选技能组合测试，模型将按需调用各 Skill 完成任务</p>
    </div>

    <div class="toolbar">
      <div class="skill-select-wrapper">
        <button class="skill-select-trigger" @click="toggleSkillDropdown" :disabled="streaming">
          <span>{{ selectedSkills.length ? `已选 ${selectedSkills.length} 个 Skill` : '选择 Skill' }}</span>
          <span class="select-arrow" :class="{ open: showSkillDropdown }">&#9662;</span>
        </button>
        <div v-if="showSkillDropdown" class="skill-select-dropdown">
          <label v-for="sk in skills" :key="sk.name" class="skill-option">
            <input type="checkbox" :value="sk.name" v-model="selectedSkills" :disabled="streaming" />
            <span class="skill-option-name">{{ sk.display_name || sk.name }}</span>
            <span class="skill-option-scope">{{ sk.scope }}</span>
          </label>
        </div>
      </div>

      <!-- Execution Mode Switch -->
      <div v-if="selectedSkills.length" class="mode-switch">
        <button
          class="mode-btn"
          :class="{ active: executionMode === 'plan' }"
          @click="executionMode = 'plan'"
          :disabled="streaming"
          title="规划模式：先生成任务清单，确认后再执行"
        >📋 规划模式</button>
        <button
          class="mode-btn"
          :class="{ active: executionMode === 'execute' }"
          @click="executionMode = 'execute'"
          :disabled="streaming"
          title="执行模式：自动规划并直接执行"
        >⚡ 执行模式</button>
      </div>

      <button class="btn-ghost" @click="resetChat" :disabled="streaming || !selectedSkills.length">
        重置对话
      </button>
      <button
        v-if="selectedSkills.length"
        class="btn-ghost btn-thoughts"
        :class="{ active: showThoughts }"
        @click="showThoughts = !showThoughts"
        title="显示/隐藏执行过程面板"
      >
        🔍 执行过程{{ thoughts.length ? ` (${thoughts.length})` : '' }}
      </button>
      <button
        v-if="selectedSkills.length && (currentPlanPreview || currentSOP)"
        class="btn-ghost btn-plan-panel"
        :class="{ active: showPlanPanel }"
        @click="showPlanPanel = !showPlanPanel"
        title="显示/隐藏方案面板"
      >
        📋 方案{{ currentPlanPreview ? ' (待确认)' : '' }}
      </button>
    </div>

    <div v-if="!selectedSkills.length" class="empty muted">
      请先勾选一个或多个 Skill 开始测试。
    </div>

    <template v-else>
      <div class="content-area">
        <!-- Main chat column -->
        <div class="messages-column">
          <div class="messages" ref="messagesEl">
            <div v-if="messages.length === 0" class="empty muted">
              <p>已勾选 <strong>{{ selectedSkills.length }}</strong> 个 Skill，模型将按需调用。</p>
              <p>向它发送消息，测试组合效果。</p>
              <p class="mode-hint" v-if="executionMode === 'plan'">
                📋 当前为 <strong>规划模式</strong>：AI 会先生成任务清单供你确认后再执行。
              </p>
              <p class="mode-hint" v-else>
                ⚡ 当前为 <strong>执行模式</strong>：AI 将自动规划并直接执行任务。
              </p>
            </div>
            <template v-for="(msg, i) in messages" :key="i">
              <!-- action result card -->
              <div
                v-if="msg.role === 'system'"
                class="action-card"
                :class="msg.success ? 'ok' : 'fail'"
                :aria-label="msg.success ? '操作成功' : '操作失败'"
              >
                <span class="action-icon">{{ msg.success ? '✅' : '❌' }}</span>
                <span class="action-label">{{ actionLabel(msg.action) }}</span>
                <span class="action-name">{{ msg.name }}</span>
                <span class="action-msg">{{ msg.message }}</span>
                <span v-if="msg.path" class="action-path">{{ msg.path }}</span>
                <pre v-if="msg.stdout" class="action-output">{{ msg.stdout }}</pre>
                <pre v-if="msg.stderr" class="action-stderr">{{ msg.stderr }}</pre>
                <div v-if="msg.output_files && msg.output_files.length" class="action-files">
                  <span class="action-files-label">📥 生成文件：</span>
                  <a
                    v-for="f in msg.output_files"
                    :key="f.url"
                    :href="f.url"
                    :download="fileBasename(f)"
                    class="action-file-link"
                  >📄 {{ fileBasename(f) }}</a>
                </div>
              </div>
              <!-- regular chat bubble -->
              <div
                v-else
                class="message"
                :class="msg.role"
              >
                <div class="bubble">
                  <AgentProcessPanel
                    v-if="msg.agentProcess"
                    :process="msg.agentProcess"
                    @ask-user-submit="handleAskUserSubmit"
                  />
                  <ChatBubble v-if="msg.content" :content="msg.content" :files="msg.files" />
                  <!-- Inline task checklist (shown when task_checklist data is attached) -->
                  <InlineTaskList
                    v-if="msg.taskChecklist"
                    :tasks="msg.taskChecklist.tasks"
                    :completed-indices="msg.taskChecklist.completedIndices"
                    :executing-index="msg.taskChecklist.executingIndex"
                  />
                </div>
              </div>
            </template>
            <div v-if="currentStatus && !agentProcess" class="status-bar" :class="`phase-${currentStatus.phase}`"
                 role="status" aria-live="polite">
              <span class="status-spinner" aria-hidden="true"></span>
              <span class="status-message">{{ currentStatus.message }}</span>
            </div>
            <div v-if="skippedSteps.length && !streaming" class="skipped-bar" role="status">
              <span class="skipped-icon">⏭️</span>
              <span class="skipped-label">已跳过 {{ skippedSteps.length }} 个步骤：</span>
              <span v-for="(s, idx) in skippedSteps" :key="idx" class="skipped-chip">
                {{ s.step }}（{{ s.reason }}）
              </span>
            </div>
            <div v-if="streaming" class="message assistant">
              <div class="bubble">
                <AgentProcessPanel
                  v-if="agentProcess"
                  :process="agentProcess"
                  @ask-user-submit="handleAskUserSubmit"
                />
                <ChatBubble :content="streamBuffer" :streaming="true" />
              </div>
            </div>
          </div>

          <div class="input-area">
            <!-- 本轮生成文件固定展示栏 -->
            <div v-if="roundOutputFiles.length" class="round-files-bar">
              <span class="round-files-label">📥 本次生成的文件</span>
              <a
                v-for="f in roundOutputFiles"
                :key="f.url"
                :href="f.url"
                :download="fileBasename(f)"
                class="round-file-link"
              >📄 {{ fileBasename(f) }}</a>
            </div>
            <div v-if="error" class="error">{{ error }}</div>
            <div v-if="uploadError" class="error">{{ uploadError }}</div>
            <!-- Document index status -->
            <div v-if="indexStatus" class="index-status">
              <span class="index-icon">🔍</span>
              <span class="index-text">{{ indexStatus }}</span>
            </div>
            <!-- Uploaded files chips -->
            <div v-if="uploadedFiles.length" class="upload-chips">
              <span
                v-for="(f, idx) in uploadedFiles"
                :key="f.path"
                class="upload-chip"
              >
                <span class="chip-icon">📄</span>
                <span class="chip-name">{{ f.filename }}</span>
                <button
                  class="chip-remove"
                  :disabled="streaming"
                  @click="removeUploadedFile(idx)"
                  :title="`移除 ${f.filename}`"
                >✕</button>
              </span>
            </div>
            <div class="row">
              <textarea
                v-model="input"
                rows="3"
                placeholder="向已加载的 Skill 发送测试消息…"
                @keydown.enter.exact.prevent="send"
                :disabled="streaming"
              />
              <div class="actions">
                <!-- Hidden file input -->
                <input
                  type="file"
                  ref="fileInputEl"
                  multiple
                  style="display:none"
                  @change="onFileSelected"
                />
                <button
                  class="btn-ghost btn-upload"
                  :disabled="streaming || uploading"
                  @click="fileInputEl.click()"
                  title="上传文件供 Skill 脚本读取"
                >
                  <span v-if="uploading">⏳</span>
                  <span v-else>📎</span>
                </button>
                <button class="btn-primary" @click="send" :disabled="streaming || !input.trim()">
                  {{ streaming ? '生成中…' : '发送' }}
                </button>
              </div>
            </div>
            <p class="hint muted">Enter 发送 · Shift+Enter 换行</p>
          </div>
        </div>

        <!-- Thinking panel sidebar -->
        <transition name="panel-slide">
          <div v-if="showThoughts" class="thinking-sidebar">
            <div class="thinking-sidebar-header">
              <span>执行过程</span>
              <button class="btn-ghost btn-close-panel" @click="showThoughts = false">✕</button>
            </div>
            <ThinkingPanel :thoughts="thoughts" />
          </div>
        </transition>

        <!-- Plan / SOP panel sidebar -->
        <transition name="panel-slide">
          <div v-if="showPlanPanel" class="thinking-sidebar plan-sidebar">
            <div class="thinking-sidebar-header">
              <div class="plan-tabs">
                <button
                  class="plan-tab"
                  :class="{ active: planTab === 'plan' }"
                  @click="planTab = 'plan'"
                >任务方案</button>
                <button
                  class="plan-tab"
                  :class="{ active: planTab === 'sop' }"
                  @click="planTab = 'sop'"
                >SOP</button>
              </div>
              <button class="btn-ghost btn-close-panel" @click="showPlanPanel = false">✕</button>
            </div>
            <TaskPlanPanel
              v-if="planTab === 'plan'"
              :plan="currentPlanPreview"
              :executing-index="executingIndex"
              :completed-indices="completedIndices"
              :confirming="confirming"
              @confirm="confirmCurrentPlan"
              @cancel="cancelCurrentPlan"
            />
            <SOPPanel
              v-if="planTab === 'sop'"
              :sop="currentSOP"
              @export="exportSOP"
            />
          </div>
        </transition>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { fetchSkills } from '../composables/useSkills.js'
import { streamChat, confirmPlan, streamConfirmResponse } from '../composables/useChat.js'
import ChatBubble from '../components/ChatBubble.vue'
import ThinkingPanel from '../components/ThinkingPanel.vue'
import TaskPlanPanel from '../components/TaskPlanPanel.vue'
import SOPPanel from '../components/SOPPanel.vue'
import InlineTaskList from '../components/InlineTaskList.vue'
import AgentProcessPanel from '../components/AgentProcessPanel.vue'

const ACTION_LABELS = {
  run_script: '运行脚本',
  init: '初始化目录',
  write: '写入 SKILL.md',
  write_file: '写入文件',
  validate: '校验格式',
  package: '打包 Skill',
  output_files: '生成文件',
}

function actionLabel(action) {
  return ACTION_LABELS[action] || action
}

function fileBasename(f) {
  return f.name || f.path.split('/').pop()
}

/** Generate a cryptographically random session ID */
function newSessionId() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID()
  }
  // Fallback for environments without crypto.randomUUID
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = (Math.random() * 16) | 0
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16)
  })
}

const skills = ref([])
const selectedSkills = ref([])  // 勾选的 Skill 名称数组
const showSkillDropdown = ref(false)
const messages = ref([])
const input = ref('')
const streaming = ref(false)
const streamBuffer = ref('')
const error = ref('')
const messagesEl = ref(null)
const currentStatus = ref(null)  // { phase, message } | null

// Execution mode: "plan" or "execute"
const executionMode = ref('execute')

// Thinking panel state
const thoughts = ref([])          // accumulated thought events for the current round
const showThoughts = ref(false)   // sidebar visibility

// Plan/SOP panel state
const showPlanPanel = ref(false)
const planTab = ref('plan') // 'plan' | 'sop'
const currentPlanPreview = ref(null) // plan_preview data from backend
const currentSOP = ref(null) // SOP document from backend
const confirming = ref(false)
const executingIndex = ref(-1)
const completedIndices = ref([])

// Inline task checklist state
const pendingChecklist = ref(null) // task_checklist data awaiting attachment to next assistant message

// File upload state
const sessionId = ref(newSessionId())
const uploadedFiles = ref([])  // [{ path, url, filename, size }]
const uploading = ref(false)
const uploadError = ref('')
const indexStatus = ref('')  // 文档索引状态提示
const fileInputEl = ref(null)

// Persistent file download bar — collects output_files from the current round
const roundOutputFiles = ref([])  // [{ path, url, name? }]

// Step-skipping state
const skippedSteps = ref([])  // [{ step, reason, ts }] for the current round

// Agent process panel state (multi-agent execution visualization)
const agentProcess = ref(null)  // { tasks: [...], phase: '...', askUser: null }

// Exclude system action-result cards from the history sent to the LLM.
const chatHistory = computed(() => messages.value.filter(m => m.role !== 'system'))

onMounted(async () => {
  skills.value = await fetchSkills('sandbox')
  // Clean up session files when the page is closed or refreshed
  window.addEventListener('beforeunload', _beforeUnloadHandler)
})

onBeforeUnmount(() => {
  // Clean up session files when navigating away from the sandbox view
  cleanupSession()
  window.removeEventListener('beforeunload', _beforeUnloadHandler)
})

/** Synchronous cleanup handler for beforeunload (uses sendBeacon for reliability) */
function _beforeUnloadHandler() {
  if (!selectedSkills.value.length || !sessionId.value) return
  const skillName = selectedSkills.value[0]
  const url = `/api/skills/${encodeURIComponent(skillName)}/sandbox-inputs/${encodeURIComponent(sessionId.value)}`
  // sendBeacon doesn't support DELETE, so we use a synchronous XMLHttpRequest as fallback
  try {
    const xhr = new XMLHttpRequest()
    xhr.open('DELETE', url, false) // synchronous
    xhr.send()
  } catch {
    // Best-effort; ignore errors during page unload
  }
}

/** Clean up the current session's files on the backend (inputs + outputs). */
async function cleanupSession() {
  if (!selectedSkills.value.length || !sessionId.value) return
  const skillName = selectedSkills.value[0]
  try {
    await fetch(
      `/api/skills/${encodeURIComponent(skillName)}/sandbox-inputs/${encodeURIComponent(sessionId.value)}`,
      { method: 'DELETE' },
    )
  } catch {
    // Best-effort cleanup; ignore network errors
  }
}

function resetChat() {
  // Clean up the old session's files before creating a new session
  cleanupSession()
  messages.value = []
  streamBuffer.value = ''
  error.value = ''
  input.value = ''
  currentStatus.value = null
  uploadedFiles.value = []
  uploadError.value = ''
  indexStatus.value = ''
  sessionId.value = newSessionId()
  roundOutputFiles.value = []
  thoughts.value = []
  skippedSteps.value = []
  currentPlanPreview.value = null
  currentSOP.value = null
  showPlanPanel.value = false
  confirming.value = false
  executingIndex.value = -1
  completedIndices.value = []
  pendingChecklist.value = null
  agentProcess.value = null
}

function toggleSkillDropdown() {
  showSkillDropdown.value = !showSkillDropdown.value
}

/** Update inline task checklist in the last assistant message when task_progress arrives */
function updateInlineChecklist(execIdx, completedIdxs) {
  // Find the last assistant message with a taskChecklist and update it
  for (let i = messages.value.length - 1; i >= 0; i--) {
    const msg = messages.value[i]
    if (msg.role === 'assistant' && msg.taskChecklist) {
      msg.taskChecklist = {
        ...msg.taskChecklist,
        executingIndex: execIdx,
        completedIndices: [...completedIdxs],
      }
      break
    }
  }
}

/** Handle agent_process events for multi-agent execution visualization */
function handleAgentProcessEvent(event) {
  if (!agentProcess.value) {
    agentProcess.value = { tasks: [], phase: 'decomposing', askUser: null }
  }
  const ap = agentProcess.value

  switch (event.type) {
    case 'master_dispatch': {
      // Master dispatches a task to a SubAgent
      const existing = ap.tasks.find(t => t.task_id === event.task_id)
      if (existing) {
        existing.status = 'running'
      } else {
        ap.tasks.push({
          task_id: event.task_id,
          sub_agent_type: event.sub_agent_type,
          description: event.description || '',
          depends_on: [],
          status: 'running',
          steps: [],
          error: null,
          replan_reason: null,
          retry_count: 0,
        })
      }
      ap.phase = 'executing'
      break
    }
    case 'sub_agent_start': {
      const task = ap.tasks.find(t => t.task_id === event.task_id)
      if (task) task.status = 'running'
      ap.phase = 'executing'
      break
    }
    case 'sub_agent_task_result': {
      const task = ap.tasks.find(t => t.task_id === event.task_id)
      if (task) {
        task.steps.push({
          action: event.action || '',
          success: event.success !== false,
          command: event.command || '',
          path: event.path || '',
          duration_ms: event.duration_ms || 0,
          output_files: event.output_files || [],
        })
      }
      break
    }
    case 'sub_agent_complete': {
      const task = ap.tasks.find(t => t.task_id === event.task_id)
      if (task) {
        task.status = event.success !== false ? 'completed' : 'failed'
      }
      // Check if all tasks are done
      const allDone = ap.tasks.every(t => t.status === 'completed' || t.status === 'failed')
      if (allDone) ap.phase = 'completed'
      break
    }
    case 'master_replan': {
      // Dynamic replanning: insert a new intermediate task
      ap.tasks.push({
        task_id: event.task_id,
        sub_agent_type: event.sub_agent_type || 'code_script',
        description: event.reason || '动态重规划任务',
        depends_on: [],
        status: 'running',
        steps: [],
        error: null,
        replan_reason: event.reason || null,
        retry_count: 0,
      })
      ap.phase = 'executing'
      break
    }
    case 'master_retry': {
      const task = ap.tasks.find(t => t.task_id === event.task_id)
      if (task) {
        task.status = 'running'
        task.retry_count = (task.retry_count || 0) + 1
        task.replan_reason = event.reason || '重试执行'
      }
      ap.phase = 'executing'
      break
    }
  }
}

/** Handle ask_user submission — send the user's response as a new message */
function handleAskUserSubmit(text) {
  if (!text.trim()) return
  // Mark ask_user as answered
  if (agentProcess.value?.askUser) {
    agentProcess.value.askUser.answered = true
  }
  // Send the user's response as a regular message
  input.value = text.trim()
  send()
}

function removeUploadedFile(idx) {
  uploadedFiles.value.splice(idx, 1)
}

async function onFileSelected(event) {
  const files = Array.from(event.target.files || [])
  event.target.value = ''  // reset so same file can be re-selected
  if (!files.length || !selectedSkills.value.length) return

  uploading.value = true
  uploadError.value = ''

  for (const file of files) {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('session_id', sessionId.value)
    try {
      const res = await fetch(
        `/api/skills/${encodeURIComponent(selectedSkills.value[0])}/sandbox-inputs`,
        { method: 'POST', body: fd }
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: `文件上传失败（${res.status}），请重试` }))
        uploadError.value = err.detail || '文件上传失败，请重试'
      } else {
        const data = await res.json()
        uploadedFiles.value.push(data)
      }
    } catch (e) {
      uploadError.value = e.message || '上传失败'
    }
  }

  uploading.value = false
}

async function scrollBottom() {
  await nextTick()
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}

async function send() {
  const text = input.value.trim()
  if (!text || streaming.value || !selectedSkills.value.length) return

  error.value = ''
  const fileAttachments = uploadedFiles.value.length
    ? uploadedFiles.value.map(f => ({ filename: f.filename, path: f.path }))
    : undefined
  messages.value.push({ role: 'user', content: text, files: fileAttachments })
  input.value = ''
  await scrollBottom()

  streaming.value = true
  streamBuffer.value = ''
  currentStatus.value = null
  roundOutputFiles.value = []
  thoughts.value = []           // clear previous round's thoughts
  skippedSteps.value = []       // clear previous round's skipped steps
  currentPlanPreview.value = null
  currentSOP.value = null
  executingIndex.value = -1
  completedIndices.value = []
  agentProcess.value = null

  // Snapshot the uploaded files for this message, then keep them until reset
  const inputFilesSnapshot = uploadedFiles.value.map(f => ({ path: f.path, filename: f.filename }))

  try {
    const url = '/api/chat/sandbox'
    const body = {
      messages: chatHistory.value,
      execution_mode: executionMode.value,
      sandbox_session_id: sessionId.value,
      skill_names: selectedSkills.value,
    }
    if (inputFilesSnapshot.length) body.input_files = inputFilesSnapshot
    for await (const chunk of streamChat(url, body)) {
      if (typeof chunk === 'string') {
        streamBuffer.value += chunk
        await scrollBottom()
      } else if (chunk.type === 'status') {
        // Handle ask_user status from multi-agent execution
        if (chunk.data && chunk.data.phase === 'ask_user') {
          // This is an ask_user event from the backend
          if (!agentProcess.value) {
            agentProcess.value = { tasks: [], phase: 'ask_user', askUser: null }
          }
          agentProcess.value.phase = 'ask_user'
          agentProcess.value.askUser = {
            message: chunk.data.message || '缺少必要信息，请补充',
            answered: false,
          }
        } else {
          currentStatus.value = chunk.data // null clears the status bar
        }
      } else if (chunk.type === 'thought') {
        thoughts.value.push(chunk.data)
        // Auto-show the panel when thoughts start arriving
        if (!showThoughts.value) showThoughts.value = true
        // Initialize agentProcess from master_decompose thought
        if (chunk.data.step === 'master_decompose' && chunk.data.data?.tasks) {
          if (!agentProcess.value) {
            agentProcess.value = { tasks: [], phase: 'decomposing', askUser: null }
          }
          const ap = agentProcess.value
          for (const t of chunk.data.data.tasks) {
            if (!ap.tasks.find(existing => existing.task_id === t.task_id)) {
              ap.tasks.push({
                task_id: t.task_id || '',
                sub_agent_type: t.sub_agent_type || 'code_script',
                description: t.description || '',
                depends_on: t.depends_on || [],
                status: 'pending',
                steps: [],
                error: null,
                replan_reason: null,
                retry_count: 0,
              })
            }
          }
          ap.phase = 'decomposing'
        }
        // Also handle master_mode thought to initialize agentProcess
        if (chunk.data.step === 'master_mode') {
          if (!agentProcess.value) {
            agentProcess.value = { tasks: [], phase: 'decomposing', askUser: null }
          }
        }
      } else if (chunk.type === 'action_result') {
        const r = chunk.data
        messages.value.push({
          role: 'system',
          action: r.action,
          name: r.name,
          success: r.success,
          message: r.message,
          path: r.path,
          stdout: r.stdout || '',
          stderr: r.stderr || '',
          exit_code: r.exit_code,
          output_files: r.output_files || [],
        })
        // Accumulate generated files for the persistent download bar
        if (r.output_files && r.output_files.length) {
          roundOutputFiles.value.push(...r.output_files)
        }
        // Detect document index status from index_documents.py output
        if (r.stdout) {
          try {
            const stdoutObj = JSON.parse(r.stdout.trim())
            if (stdoutObj.indexed_files !== undefined) {
              indexStatus.value = `已索引 ${stdoutObj.indexed_files} 个文档，共 ${stdoutObj.total_chunks} 个段落`
            }
          } catch { /* not JSON, ignore */ }
        }
        await scrollBottom()
      } else if (chunk.type === 'plan_preview') {
        currentPlanPreview.value = chunk.data
        showPlanPanel.value = true
        planTab.value = 'plan'
      } else if (chunk.type === 'sop_plan') {
        currentSOP.value = chunk.data
      } else if (chunk.type === 'task_progress') {
        executingIndex.value = chunk.data.executing_index
        completedIndices.value = chunk.data.completed_indices
        // Update inline task checklist in the last assistant message
        updateInlineChecklist(chunk.data.executing_index, chunk.data.completed_indices)
      } else if (chunk.type === 'task_checklist') {
        // Store checklist data to attach to the next assistant message
        pendingChecklist.value = chunk.data
      } else if (chunk.type === 'sandbox_retry') {
        // Show retry notification as a thought
        thoughts.value.push({
          step: 'sandbox_retry',
          label: `重试 (${chunk.data.attempt}/${chunk.data.max_retries})`,
          detail: chunk.data.corrected ? '已根据错误信息调整输入' : '重试执行',
          data: chunk.data,
          ts: chunk.data.ts,
        })
        if (!showThoughts.value) showThoughts.value = true
      } else if (chunk.type === 'step_skipped') {
        // Track skipped steps for display
        skippedSteps.value.push(chunk.data)
        // Also show as a thought
        thoughts.value.push({
          step: `step_skipped_${chunk.data.step}`,
          label: `跳过：${chunk.data.step}`,
          detail: chunk.data.reason,
          data: chunk.data,
          ts: chunk.data.ts,
        })
        if (!showThoughts.value) showThoughts.value = true
      } else if (chunk.type === 'agent_process') {
        // Multi-agent execution process events
        handleAgentProcessEvent(chunk.data)
      }
    }
    if (streamBuffer.value) {
      const msg = { role: 'assistant', content: streamBuffer.value }
      // Attach pending checklist data if available
      if (pendingChecklist.value) {
        msg.taskChecklist = pendingChecklist.value
        pendingChecklist.value = null
      }
      // Attach agent process data for rendering in the final message
      if (agentProcess.value) {
        msg.agentProcess = { ...agentProcess.value }
      }
      messages.value.push(msg)
      streamBuffer.value = ''
    } else if (agentProcess.value && !streamBuffer.value) {
      // No text content but agent process exists (e.g., ask_user only)
      const msg = { role: 'assistant', content: '', agentProcess: { ...agentProcess.value } }
      messages.value.push(msg)
    }
  } catch (e) {
    error.value = e.message
  } finally {
    streaming.value = false
    currentStatus.value = null
    await scrollBottom()
  }
}

/** Confirm a pending plan in Plan mode */
async function confirmCurrentPlan() {
  if (!currentPlanPreview.value || confirming.value) return

  const planId = currentPlanPreview.value.plan_id
  confirming.value = true
  streaming.value = true
  streamBuffer.value = ''
  thoughts.value = []
  roundOutputFiles.value = []
  executingIndex.value = -1
  completedIndices.value = []

  try {
    const response = await confirmPlan(selectedSkills.value[0], planId, 'confirm')

    // Check if it's a streaming response (SSE) or JSON
    const contentType = response.headers.get('content-type') || ''
    if (contentType.includes('text/event-stream')) {
      for await (const chunk of streamConfirmResponse(response)) {
        if (typeof chunk === 'string') {
          streamBuffer.value += chunk
          await scrollBottom()
        } else if (chunk.type === 'status') {
          currentStatus.value = chunk.data
        } else if (chunk.type === 'thought') {
          thoughts.value.push(chunk.data)
          if (!showThoughts.value) showThoughts.value = true
        } else if (chunk.type === 'action_result') {
          const r = chunk.data
          messages.value.push({
            role: 'system',
            action: r.action,
            name: r.name,
            success: r.success,
            message: r.message,
            path: r.path,
            stdout: r.stdout || '',
            stderr: r.stderr || '',
            exit_code: r.exit_code,
            output_files: r.output_files || [],
          })
          if (r.output_files && r.output_files.length) {
            roundOutputFiles.value.push(...r.output_files)
          }
          await scrollBottom()
        } else if (chunk.type === 'task_progress') {
          executingIndex.value = chunk.data.executing_index
          completedIndices.value = chunk.data.completed_indices
        }
      }
      if (streamBuffer.value) {
        messages.value.push({ role: 'assistant', content: streamBuffer.value })
        streamBuffer.value = ''
      }
    } else {
      // JSON response (e.g., cancel confirmation)
      const data = await response.json()
      messages.value.push({ role: 'assistant', content: data.message || '方案已执行完成。' })
    }

    currentPlanPreview.value = null
  } catch (e) {
    error.value = e.message
  } finally {
    confirming.value = false
    streaming.value = false
    currentStatus.value = null
    executingIndex.value = -1
    completedIndices.value = []
    await scrollBottom()
  }
}

/** Cancel a pending plan */
async function cancelCurrentPlan() {
  if (!currentPlanPreview.value) return

  const planId = currentPlanPreview.value.plan_id
  try {
    await confirmPlan(selectedSkills.value[0], planId, 'cancel')
    messages.value.push({ role: 'assistant', content: '❌ 执行方案已取消。' })
    currentPlanPreview.value = null
  } catch (e) {
    error.value = e.message
  }
}

/** Export SOP in the specified format */
async function exportSOP(format) {
  if (!currentSOP.value) return

  if (format === 'json') {
    const blob = new Blob([JSON.stringify(currentSOP.value, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `sop-${selectedSkills.value[0]}.json`
    a.click()
    URL.revokeObjectURL(url)
  } else {
    // Request markdown export from backend if plan_id available, otherwise format locally
    const planId = currentPlanPreview.value?.plan_id
    if (planId) {
      try {
        const res = await fetch(`/api/chat/sandbox/${encodeURIComponent(selectedSkills.value[0])}/sop`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ plan_id: planId, format: 'markdown' }),
        })
        if (res.ok) {
          const data = await res.json()
          const blob = new Blob([data.content], { type: 'text/markdown' })
          const url = URL.createObjectURL(blob)
          const a = document.createElement('a')
          a.href = url
          a.download = `sop-${selectedSkills.value[0]}.md`
          a.click()
          URL.revokeObjectURL(url)
          return
        }
      } catch (_) { /* fallthrough to local export */ }
    }

    // Local markdown generation fallback
    const sop = currentSOP.value
    let md = `# ${sop.title}\n\n`
    md += `**版本**：${sop.version}\n**复杂度**：${sop.complexity}\n\n## 步骤\n\n`
    for (const step of (sop.steps || [])) {
      md += `### ${step.order}. ${step.name}\n${step.description}\n\n`
    }
    if (sop.flowchart_mermaid) {
      md += `\n## 流程图\n\n\`\`\`mermaid\n${sop.flowchart_mermaid}\n\`\`\`\n`
    }
    const blob = new Blob([md], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `sop-${selectedSkill.value}.md`
    a.click()
    URL.revokeObjectURL(url)
  }
}
</script>

<style scoped>
/*
 * CSS custom properties for the thinking sidebar so magic numbers
 * are defined in one place and reused across layout + transitions.
 */
.sandbox {
  --thinking-sidebar-width: 320px;
  --thinking-sidebar-mobile-height: 280px;
  --thinking-breakpoint: 900px;

  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.header {
  padding: 20px 24px 12px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.header h2 { font-size: 18px; font-weight: 600; margin-bottom: 4px; }

.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 24px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.toolbar select { max-width: 280px; }

/* Skill dropdown select */
.skill-select-wrapper {
  position: relative;
  min-width: 200px;
}

.skill-select-trigger {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
  transition: border-color 0.15s;
}

.skill-select-trigger:hover:not(:disabled) {
  border-color: var(--accent);
}

.skill-select-trigger:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.select-arrow {
  font-size: 10px;
  transition: transform 0.2s;
}

.select-arrow.open {
  transform: rotate(180deg);
}

.skill-select-dropdown {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  min-width: 100%;
  max-height: 240px;
  overflow-y: auto;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  z-index: 100;
  padding: 4px 0;
}

.skill-option {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 13px;
  transition: background 0.15s;
}

.skill-option:hover {
  background: rgba(255, 255, 255, 0.06);
}

.skill-option input[type="checkbox"] {
  accent-color: var(--accent);
  width: 14px;
  height: 14px;
}

.skill-option-name {
  flex: 1;
  color: var(--text);
  font-weight: 500;
}

.skill-option-scope {
  font-size: 11px;
  color: var(--text-muted);
  padding: 1px 6px;
  border-radius: 3px;
  background: rgba(255, 255, 255, 0.06);
}

.btn-thoughts {
  margin-left: auto;
  font-size: 13px;
  padding: 5px 12px;
  border-radius: 6px;
  color: var(--text);
  transition: background 0.15s, color 0.15s;
}
.btn-thoughts.active {
  background: var(--color-blue-bg);
  color: var(--color-blue-text);
  border-color: var(--color-blue-border);
}

.empty {
  margin: auto;
  text-align: center;
  max-width: 400px;
  padding: 40px 0;
  line-height: 2;
}

/* Main two-column layout */
.content-area {
  flex: 1;
  display: flex;
  overflow: hidden;
  min-height: 0;
}

.messages-column {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
}

/* Thinking panel sidebar */
.thinking-sidebar {
  width: var(--thinking-sidebar-width);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--border);
  background: var(--surface);
  overflow: hidden;
}

.thinking-sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  font-size: 13px;
  font-weight: 600;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  color: var(--text);
}

.btn-close-panel {
  font-size: 12px;
  padding: 2px 6px;
  line-height: 1;
}

/* Slide-in transition for the sidebar */
.panel-slide-enter-active,
.panel-slide-leave-active {
  transition: width 0.25s ease, opacity 0.2s ease;
  overflow: hidden;
}
.panel-slide-enter-from,
.panel-slide-leave-to {
  width: 0;
  opacity: 0;
}
.panel-slide-enter-to,
.panel-slide-leave-from {
  width: var(--thinking-sidebar-width);
  opacity: 1;
}

/* On narrow viewports, sidebar stacks below the chat */
@media (max-width: 900px) {
  .content-area { flex-direction: column; }

  .thinking-sidebar {
    width: 100%;
    border-left: none;
    border-top: 1px solid var(--border);
    max-height: var(--thinking-sidebar-mobile-height);
  }

  .panel-slide-enter-from,
  .panel-slide-leave-to {
    width: 100%;
    max-height: 0;
    opacity: 0;
  }

  .panel-slide-enter-to,
  .panel-slide-leave-from {
    width: 100%;
    max-height: var(--thinking-sidebar-mobile-height);
    opacity: 1;
  }
}

.messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px 24px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.message { display: flex; }
.message.user { justify-content: flex-end; }
.message.assistant { justify-content: flex-start; }

.bubble {
  max-width: 72%;
  padding: 12px 16px;
  border-radius: 12px;
  background: var(--surface2);
  border: 1px solid var(--border);
}
.message.user .bubble {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}

/* Action result card */
.action-card {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 10px;
  padding: 10px 14px;
  border-radius: 8px;
  font-size: 13px;
  border: 1px solid transparent;
}
.action-card.ok {
  background: var(--color-green-bg);
  border-color: var(--color-green-border);
  color: var(--color-green-text);
}
.action-card.fail {
  background: var(--color-red-bg);
  border-color: var(--color-red-border);
  color: var(--color-red-text);
}
.action-icon { font-size: 15px; }
.action-label { font-weight: 600; }
.action-name { font-family: monospace; background: rgba(255,255,255,.07); padding: 1px 6px; border-radius: 4px; }
.action-msg { flex: 1 1 100%; margin-top: 2px; opacity: .85; }
.action-path { flex: 1 1 100%; font-family: monospace; font-size: 12px; opacity: .7; word-break: break-all; }
.action-output, .action-stderr {
  flex: 1 1 100%;
  margin: 4px 0 0;
  padding: 6px 8px;
  border-radius: 4px;
  font-family: 'Fira Code', 'Cascadia Code', monospace;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 200px;
  overflow-y: auto;
}
.action-output { background: rgba(255,255,255,.06); }
.action-stderr { background: rgba(239,68,68,.1); }

/* Output file download links */
.action-files {
  flex: 1 1 100%;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px solid rgba(255,255,255,.08);
}
.action-files-label {
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}
.action-file-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 10px;
  border-radius: 4px;
  background: rgba(255,255,255,.07);
  font-size: 12px;
  font-family: monospace;
  color: inherit;
  text-decoration: none;
  border: 1px solid rgba(255,255,255,.12);
  transition: background 0.15s;
}
.action-file-link:hover {
  background: rgba(255,255,255,.14);
  text-decoration: underline;
}

/* Persistent generated-files bar above the input area */
.round-files-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  padding: 8px 12px;
  margin-bottom: 8px;
  border-radius: 8px;
  background: var(--color-blue-bg);
  border: 1px solid var(--color-blue-border);
  color: var(--color-blue-text);
}
.round-files-label {
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  flex-shrink: 0;
}
.round-file-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 10px;
  border-radius: 4px;
  background: rgba(108,138,255,.1);
  font-size: 12px;
  font-family: monospace;
  color: var(--color-blue-text);
  text-decoration: none;
  border: 1px solid var(--color-blue-border);
  transition: background 0.15s;
}
.round-file-link:hover {
  background: rgba(108,138,255,.2);
  text-decoration: underline;
}

.input-area {
  padding: 12px 24px 20px;
  border-top: 1px solid var(--border);
  flex-shrink: 0;
}
.row { display: flex; gap: 12px; align-items: flex-end; }
.row textarea { flex: 1; min-height: 72px; }
.actions { display: flex; flex-direction: column; gap: 8px; }
.hint { font-size: 12px; margin-top: 6px; }

/* Upload chips */
.upload-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.index-status {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border-radius: 6px;
  background: rgba(34, 197, 94, 0.1);
  border: 1px solid rgba(34, 197, 94, 0.3);
  font-size: 12px;
  color: #16a34a;
  margin-bottom: 8px;
}
.index-icon { font-size: 13px; }
.index-text { font-family: var(--font); }
.upload-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px 2px 6px;
  border-radius: 12px;
  background: var(--surface2);
  border: 1px solid var(--border);
  font-size: 12px;
  font-family: monospace;
  max-width: 240px;
}
.chip-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 180px;
}
.chip-remove {
  background: none;
  border: none;
  cursor: pointer;
  padding: 0 2px;
  font-size: 11px;
  line-height: 1;
  color: var(--text);
  opacity: 0.5;
  transition: opacity 0.15s;
}
.chip-remove:hover:not(:disabled) { opacity: 1; }
.chip-remove:disabled { cursor: not-allowed; }

/* Upload button */
.btn-upload {
  padding: 6px 10px;
  font-size: 16px;
  line-height: 1;
}

/* Execution status bar */
.status-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 14px;
  border-radius: 8px;
  font-size: 13px;
  border: 1px solid var(--border);
  background: var(--surface2);
  color: var(--text);
  animation: status-fade-in 0.2s ease;
}
.status-bar.phase-analyzing    { border-color: var(--color-blue-border); background: var(--color-blue-bg); color: var(--color-blue-text); }
.status-bar.phase-loading,
.status-bar.phase-loading_child,
.status-bar.phase-loading_resources { border-color: var(--color-purple-border); background: var(--color-purple-bg); color: var(--color-purple-text); }
.status-bar.phase-planning     { border-color: var(--color-yellow-border); background: var(--color-yellow-bg); color: var(--color-yellow-text); }
.status-bar.phase-executing    { border-color: var(--color-green-border); background: var(--color-green-bg); color: var(--color-green-text); }
.status-bar.phase-reading      { border-color: var(--color-blue-border); background: var(--color-blue-bg); color: var(--color-blue-text); }
.status-bar.phase-writing      { border-color: var(--color-orange-border); background: var(--color-orange-bg); color: var(--color-orange-text); }
.status-bar.phase-creating     { border-color: var(--color-red-border); background: var(--color-red-bg); color: var(--color-red-text); }
.status-bar.phase-generating   { border-color: var(--color-green-border); background: var(--color-green-bg); color: var(--color-green-text); }

.status-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  flex-shrink: 0;
  opacity: 0.7;
}
.status-message { flex: 1; }

/* Skipped steps bar */
.skipped-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  padding: 7px 14px;
  border-radius: 8px;
  font-size: 13px;
  border: 1px solid var(--color-green-border);
  background: var(--color-green-bg);
  color: var(--color-green-text);
  animation: status-fade-in 0.2s ease;
}
.skipped-icon { font-size: 14px; flex-shrink: 0; }
.skipped-label { font-weight: 600; white-space: nowrap; }
.skipped-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 8px;
  border-radius: 4px;
  background: rgba(34,197,94,0.15);
  font-size: 12px;
  font-family: monospace;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
@keyframes status-fade-in {
  from { opacity: 0; transform: translateY(4px); }
  to   { opacity: 1; transform: translateY(0); }
}
</style>
