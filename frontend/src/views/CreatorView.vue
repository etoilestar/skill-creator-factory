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
      <div
        v-for="(msg, i) in messages"
        :key="i"
        class="message"
        :class="msg.role"
      >
        <div class="bubble">
          <pre class="content">{{ msg.content }}</pre>
        </div>
      </div>
      <div v-if="streaming" class="message assistant">
        <div class="bubble">
          <pre class="content">{{ streamBuffer }}<span class="cursor">▋</span></pre>
        </div>
      </div>
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
import { ref, nextTick } from 'vue'
import { streamChat } from '../composables/useChat.js'

const messages = ref([])
const input = ref('')
const streaming = ref(false)
const streamBuffer = ref('')
const error = ref('')
const messagesEl = ref(null)

async function scrollBottom() {
  await nextTick()
  if (messagesEl.value) {
    messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  }
}

async function send() {
  const text = input.value.trim()
  if (!text || streaming.value) return

  error.value = ''
  messages.value.push({ role: 'user', content: text })
  input.value = ''
  await scrollBottom()

  streaming.value = true
  streamBuffer.value = ''

  try {
    for await (const chunk of streamChat('/api/chat/creator', { messages: messages.value })) {
      streamBuffer.value += chunk
      await scrollBottom()
    }
    messages.value.push({ role: 'assistant', content: streamBuffer.value })
    streamBuffer.value = ''
  } catch (e) {
    error.value = e.message
  } finally {
    streaming.value = false
    await scrollBottom()
  }
}

function clearChat() {
  messages.value = []
  streamBuffer.value = ''
  error.value = ''
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

.content {
  font-family: var(--font);
  font-size: 14px;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}

.cursor { animation: blink 1s step-end infinite; }
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }

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
