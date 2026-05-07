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
    </div>

    <div v-if="!selectedSkill" class="empty muted">
      请先选择一个 Skill 开始测试。
    </div>

    <template v-else>
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
                :download="f.name || f.path.split('/').pop()"
                class="action-file-link"
              >📄 {{ f.name || f.path.split('/').pop() }}</a>
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
        <div v-if="streaming" class="message assistant">
          <div class="bubble">
            <ChatBubble :content="streamBuffer" :streaming="true" />
          </div>
        </div>
      </div>

      <div class="input-area">
        <div v-if="error" class="error">{{ error }}</div>
        <div class="row">
          <textarea
            v-model="input"
            rows="3"
            placeholder="向已加载的 Skill 发送测试消息…"
            @keydown.enter.exact.prevent="send"
            :disabled="streaming"
          />
          <div class="actions">
            <button class="btn-primary" @click="send" :disabled="streaming || !input.trim()">
              {{ streaming ? '生成中…' : '发送' }}
            </button>
          </div>
        </div>
        <p class="hint muted">Enter 发送 · Shift+Enter 换行</p>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, nextTick } from 'vue'
import { fetchSkills } from '../composables/useSkills.js'
import { streamChat } from '../composables/useChat.js'
import ChatBubble from '../components/ChatBubble.vue'

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

const skills = ref([])
const selectedSkill = ref('')
const messages = ref([])
const input = ref('')
const streaming = ref(false)
const streamBuffer = ref('')
const error = ref('')
const messagesEl = ref(null)

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

  try {
    const url = `/api/chat/sandbox/${encodeURIComponent(selectedSkill.value)}`
    for await (const chunk of streamChat(url, { messages: chatHistory.value })) {
      if (typeof chunk === 'string') {
        streamBuffer.value += chunk
        await scrollBottom()
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
    await scrollBottom()
  }
}
</script>

<style scoped>
.sandbox { display: flex; flex-direction: column; height: 100%; overflow: hidden; }

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

.empty {
  margin: auto;
  text-align: center;
  max-width: 400px;
  padding: 40px 0;
  line-height: 2;
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

.input-area {
  padding: 12px 24px 20px;
  border-top: 1px solid var(--border);
  flex-shrink: 0;
}
.row { display: flex; gap: 12px; align-items: flex-end; }
.row textarea { flex: 1; min-height: 72px; }
.actions { display: flex; flex-direction: column; gap: 8px; }
.hint { font-size: 12px; margin-top: 6px; }
</style>
