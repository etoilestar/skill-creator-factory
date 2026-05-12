<template>
  <div class="sandbox">
    <div class="header">
      <h2>Sandbox</h2>
      <p class="muted">选择一个 Skill，模拟它被加载后的对话效果</p>
    </div>

    <div class="toolbar">
      <select v-model="selectedSkill" @change="resetChat" :disabled="streaming">
        <option value="">-- 选择 Skill --</option>
        <option v-for="sk in skills" :key="sk.name" :value="sk.name">
          {{ sk.display_name || sk.name }}
        </option>
      </select>
      <button class="btn-ghost" @click="resetChat" :disabled="streaming || !selectedSkill">
        重置对话
      </button>
      <button
        v-if="selectedSkill"
        class="btn-ghost btn-thoughts"
        :class="{ active: showThoughts }"
        @click="showThoughts = !showThoughts"
        title="显示/隐藏执行过程面板"
      >
        🔍 执行过程{{ thoughts.length ? ` (${thoughts.length})` : '' }}
      </button>
    </div>

    <div v-if="!selectedSkill" class="empty muted">
      请先选择一个 Skill 开始测试。
    </div>

    <template v-else>
      <div class="content-area">
        <!-- Main chat column -->
        <div class="messages-column">
          <div class="messages" ref="messagesEl">
            <div v-if="messages.length === 0" class="empty muted">
              <p>Skill <strong>{{ selectedSkill }}</strong> 已加载为 system prompt。</p>
              <p>向它发送消息，测试它的行为。</p>
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
                  <ChatBubble :content="msg.content" />
                </div>
              </div>
            </template>
            <div v-if="currentStatus" class="status-bar" :class="`phase-${currentStatus.phase}`"
                 role="status" aria-live="polite">
              <span class="status-spinner" aria-hidden="true"></span>
              <span class="status-message">{{ currentStatus.message }}</span>
            </div>
            <div v-if="streaming" class="message assistant">
              <div class="bubble">
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
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, nextTick } from 'vue'
import { fetchSkills } from '../composables/useSkills.js'
import { streamChat } from '../composables/useChat.js'
import ChatBubble from '../components/ChatBubble.vue'
import ThinkingPanel from '../components/ThinkingPanel.vue'

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
const selectedSkill = ref('')
const messages = ref([])
const input = ref('')
const streaming = ref(false)
const streamBuffer = ref('')
const error = ref('')
const messagesEl = ref(null)
const currentStatus = ref(null)  // { phase, message } | null

// Thinking panel state
const thoughts = ref([])          // accumulated thought events for the current round
const showThoughts = ref(false)   // sidebar visibility

// File upload state
const sessionId = ref(newSessionId())
const uploadedFiles = ref([])  // [{ path, url, filename, size }]
const uploading = ref(false)
const uploadError = ref('')
const fileInputEl = ref(null)

// Persistent file download bar — collects output_files from the current round
const roundOutputFiles = ref([])  // [{ path, url, name? }]

// Exclude system action-result cards from the history sent to the LLM.
const chatHistory = computed(() => messages.value.filter(m => m.role !== 'system'))

onMounted(async () => {
  skills.value = await fetchSkills()
})

function resetChat() {
  messages.value = []
  streamBuffer.value = ''
  error.value = ''
  input.value = ''
  currentStatus.value = null
  uploadedFiles.value = []
  uploadError.value = ''
  sessionId.value = newSessionId()
  roundOutputFiles.value = []
  thoughts.value = []
}

function removeUploadedFile(idx) {
  uploadedFiles.value.splice(idx, 1)
}

async function onFileSelected(event) {
  const files = Array.from(event.target.files || [])
  event.target.value = ''  // reset so same file can be re-selected
  if (!files.length || !selectedSkill.value) return

  uploading.value = true
  uploadError.value = ''

  for (const file of files) {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('session_id', sessionId.value)
    try {
      const res = await fetch(
        `/api/skills/${encodeURIComponent(selectedSkill.value)}/sandbox-inputs`,
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
  if (!text || streaming.value || !selectedSkill.value) return

  error.value = ''
  messages.value.push({ role: 'user', content: text })
  input.value = ''
  await scrollBottom()

  streaming.value = true
  streamBuffer.value = ''
  currentStatus.value = null
  roundOutputFiles.value = []
  thoughts.value = []           // clear previous round's thoughts

  // Snapshot the uploaded files for this message, then keep them until reset
  const inputFilesSnapshot = uploadedFiles.value.map(f => ({ path: f.path, filename: f.filename }))

  try {
    const url = `/api/chat/sandbox/${encodeURIComponent(selectedSkill.value)}`
    const body = { messages: chatHistory.value }
    if (inputFilesSnapshot.length) body.input_files = inputFilesSnapshot
    for await (const chunk of streamChat(url, body)) {
      if (typeof chunk === 'string') {
        streamBuffer.value += chunk
        await scrollBottom()
      } else if (chunk.type === 'status') {
        currentStatus.value = chunk.data  // null clears the status bar
      } else if (chunk.type === 'thought') {
        thoughts.value.push(chunk.data)
        // Auto-show the panel when thoughts start arriving
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
        // Accumulate generated files for the persistent download bar
        if (r.output_files && r.output_files.length) {
          roundOutputFiles.value.push(...r.output_files)
        }
        await scrollBottom()
      }
    }
    if (streamBuffer.value) {
      messages.value.push({ role: 'assistant', content: streamBuffer.value })
      streamBuffer.value = ''
    }
  } catch (e) {
    error.value = e.message
  } finally {
    streaming.value = false
    currentStatus.value = null
    await scrollBottom()
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

.btn-thoughts {
  margin-left: auto;
  font-size: 13px;
  padding: 5px 12px;
  border-radius: 6px;
  color: var(--text);
  transition: background 0.15s, color 0.15s;
}
.btn-thoughts.active {
  background: #eff6ff;
  color: #1e40af;
  border-color: #bfdbfe;
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
  background: #f0fdf4;
  border-color: #bbf7d0;
  color: #166534;
}
.action-card.fail {
  background: #fef2f2;
  border-color: #fecaca;
  color: #991b1b;
}
.action-icon { font-size: 15px; }
.action-label { font-weight: 600; }
.action-name { font-family: monospace; background: rgba(0,0,0,.07); padding: 1px 6px; border-radius: 4px; }
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
.action-output { background: rgba(0,0,0,.06); }
.action-stderr { background: rgba(200,0,0,.07); }

/* Output file download links */
.action-files {
  flex: 1 1 100%;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px solid rgba(0,0,0,.08);
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
  background: rgba(0,0,0,.07);
  font-size: 12px;
  font-family: monospace;
  color: inherit;
  text-decoration: none;
  border: 1px solid rgba(0,0,0,.12);
  transition: background 0.15s;
}
.action-file-link:hover {
  background: rgba(0,0,0,.14);
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
  background: #eff6ff;
  border: 1px solid #bfdbfe;
  color: #1e40af;
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
  background: rgba(30,64,175,.1);
  font-size: 12px;
  font-family: monospace;
  color: #1e40af;
  text-decoration: none;
  border: 1px solid rgba(30,64,175,.25);
  transition: background 0.15s;
}
.round-file-link:hover {
  background: rgba(30,64,175,.2);
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
.status-bar.phase-analyzing    { border-color: #bfdbfe; background: #eff6ff; color: #1e40af; }
.status-bar.phase-loading,
.status-bar.phase-loading_child,
.status-bar.phase-loading_resources { border-color: #e9d5ff; background: #faf5ff; color: #6b21a8; }
.status-bar.phase-planning     { border-color: #fde68a; background: #fffbeb; color: #92400e; }
.status-bar.phase-executing    { border-color: #bbf7d0; background: #f0fdf4; color: #166534; }
.status-bar.phase-reading      { border-color: #bae6fd; background: #f0f9ff; color: #075985; }
.status-bar.phase-writing      { border-color: #fed7aa; background: #fff7ed; color: #9a3412; }
.status-bar.phase-creating     { border-color: #fecdd3; background: #fff1f2; color: #9f1239; }
.status-bar.phase-generating   { border-color: #d9f99d; background: #f7fee7; color: #365314; }

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

@keyframes spin {
  to { transform: rotate(360deg); }
}
@keyframes status-fade-in {
  from { opacity: 0; transform: translateY(4px); }
  to   { opacity: 1; transform: translateY(0); }
}
</style>
