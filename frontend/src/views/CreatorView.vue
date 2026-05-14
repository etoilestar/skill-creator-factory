<template>
  <div class="creator">
    <div class="header">
      <h2>Skill Creator</h2>
      <p class="muted">Powered by <code>kernel/SKILL.md</code> · multi-turn conversation</p>
    </div>

    <div class="messages" ref="messagesEl">
      <div v-if="messages.length === 0" class="empty">
        <p>向 AI 说明你想创建什么 Skill，它会引导你一步步完成。</p>
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
        </div>
        <!-- regular chat bubble -->
        <div v-else class="message" :class="msg.role">
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

      <!-- Skill creation panel (shown after user confirms blueprint) -->
      <SkillCreationPanel
        v-if="showCreationPanel && creationPlan"
        :skill-name="creationPlan.skill_name"
        :files="creationPlan.files"
        :blueprint-text="blueprintText"
        :conversation-history="chatHistory"
        :model="null"
        :warnings="creationPlan.warnings"
        @creation-complete="onCreationComplete"
        @creation-error="onCreationError"
      />
    </div>

    <div class="input-area">
      <div v-if="error" class="error">{{ error }}</div>
      <div class="row">
        <textarea
          v-model="input"
          rows="3"
          placeholder="输入你的想法…"
          @keydown.enter.exact.prevent="send"
          :disabled="streaming"
        />
        <div class="actions">
          <button class="btn-primary" @click="send" :disabled="streaming || !input.trim()">
            {{ streaming ? '生成中…' : '发送' }}
          </button>
          <button class="btn-ghost" @click="clearChat" :disabled="streaming">清空</button>
        </div>
      </div>
      <p class="hint muted">Enter 发送 · Shift+Enter 换行</p>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick } from 'vue'
import { streamChat } from '../composables/useChat.js'
import { analyzeBlueprintPlan } from '../composables/useCreator.js'
import ChatBubble from '../components/ChatBubble.vue'
import SkillCreationPanel from '../components/SkillCreationPanel.vue'

// ---------------------------------------------------------------------------
// Keywords kept in sync with backend/_CONFIRM_KEYWORDS and _BLUEPRINT_MARKERS
// ---------------------------------------------------------------------------
const CONFIRM_KEYWORDS = [
  '对，开始做吧',
  '开始做吧',
  '开始创建',
  '开始生成',
  '确认，开始',
  '确认开始',
  '可以开始',
  '没问题，开始',
]
const BLUEPRINT_MARKER = '📋 Skill 蓝图'

function isCreationConfirmation(text) {
  return CONFIRM_KEYWORDS.some(kw => text.includes(kw))
}

function hasBlueprintInHistory() {
  return messages.value.some(
    m => m.role === 'assistant' && (m.content || '').includes(BLUEPRINT_MARKER)
  )
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const ACTION_LABELS = {
  init: '初始化目录',
  write: '写入 SKILL.md',
  write_file: '写入文件',
  validate: '校验格式',
  package: '打包 Skill',
  run_script: '运行脚本',
}

function actionLabel(action) {
  return ACTION_LABELS[action] || action
}

const messages = ref([])
const input = ref('')
const streaming = ref(false)
const streamBuffer = ref('')
const error = ref('')
const messagesEl = ref(null)

// Creation panel state
const showCreationPanel = ref(false)
const creationPlan = ref(null)

// The raw blueprint text extracted from the latest blueprint assistant message
const blueprintText = computed(() => {
  for (let i = messages.value.length - 1; i >= 0; i--) {
    const m = messages.value[i]
    if (m.role === 'assistant' && (m.content || '').includes(BLUEPRINT_MARKER)) {
      return m.content
    }
  }
  return ''
})

// History sent to the LLM excludes system action-result messages
const chatHistory = computed(() => messages.value.filter(m => m.role !== 'system'))

async function scrollBottom() {
  await nextTick()
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}

// ---------------------------------------------------------------------------
// Send
// ---------------------------------------------------------------------------

async function send() {
  const text = input.value.trim()
  if (!text || streaming.value) return

  error.value = ''
  messages.value.push({ role: 'user', content: text })
  input.value = ''
  await scrollBottom()

  // If the user just confirmed the blueprint, trigger blueprint analysis in parallel
  // with the chat call (pure-rule extraction, no LLM, very fast).
  if (isCreationConfirmation(text) && hasBlueprintInHistory()) {
    analyzeBlueprintPlan(chatHistory.value)
      .then(plan => {
        creationPlan.value = plan
        showCreationPanel.value = true
        nextTick(scrollBottom)
      })
      .catch(err => {
        error.value = `蓝图解析失败：${err.message}，请重试`
      })
  }

  streaming.value = true
  streamBuffer.value = ''

  try {
    for await (const chunk of streamChat('/api/chat/creator', { messages: chatHistory.value })) {
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

// ---------------------------------------------------------------------------
// Creation panel handlers
// ---------------------------------------------------------------------------

function onCreationComplete({ skillName }) {
  messages.value.push({
    role: 'assistant',
    content: `✅ Skill **${skillName}** 已创建完成！可以在沙盒模式下测试。`,
  })
  showCreationPanel.value = false
  scrollBottom()
}

function onCreationError(errMsg) {
  error.value = `Skill 创建失败：${errMsg}`
}

// ---------------------------------------------------------------------------
// Clear
// ---------------------------------------------------------------------------

function clearChat() {
  messages.value = []
  streamBuffer.value = ''
  error.value = ''
  showCreationPanel.value = false
  creationPlan.value = null
}
</script>

<style scoped>
.creator {
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

.messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px 24px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.empty {
  margin: auto;
  text-align: center;
  color: var(--text-muted);
  max-width: 400px;
  padding: 40px 0;
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

