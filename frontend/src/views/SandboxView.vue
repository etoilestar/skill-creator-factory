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
        <div
          v-for="(msg, i) in messages"
          :key="i"
          class="message"
          :class="msg.role"
        >
          <div class="bubble">
            <ChatBubble :content="msg.content" />
          </div>
        </div>
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
import { ref, onMounted, nextTick } from 'vue'
import { fetchSkills } from '../composables/useSkills.js'
import { streamChat } from '../composables/useChat.js'
import ChatBubble from '../components/ChatBubble.vue'

const skills = ref([])
const selectedSkill = ref('')
const messages = ref([])
const input = ref('')
const streaming = ref(false)
const streamBuffer = ref('')
const error = ref('')
const messagesEl = ref(null)

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
    for await (const chunk of streamChat(url, { messages: messages.value })) {
      if (typeof chunk !== 'string') continue
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
