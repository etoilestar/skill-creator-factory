<template>
  <div class="layout">
    <nav class="sidebar">
      <div class="brand">
        <span class="brand-icon">⚡</span>
        <span class="brand-name">Skill Factory</span>
      </div>
      <RouterLink to="/creator" class="nav-item">
        <span>🛠</span> Creator
      </RouterLink>
      <RouterLink to="/skills" class="nav-item">
        <span>📚</span> Skills
      </RouterLink>
      <RouterLink to="/sandbox" class="nav-item">
        <span>🧪</span> Sandbox
      </RouterLink>
      <div class="sidebar-footer">
        <div class="llm-status" :class="llmStatus">
          <span class="dot"></span>
          {{ llmLabel }}
        </div>
      </div>
    </nav>
    <main class="content">
      <RouterView />
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { fetchLlmHealth } from './composables/useSkills.js'

const llmStatus = ref('unknown')
const llmLabel = ref('Checking LLM…')

onMounted(async () => {
  try {
    const data = await fetchLlmHealth()
    if (data.connected) {
      llmStatus.value = 'ok'
      llmLabel.value = `LLM connected`
    } else {
      llmStatus.value = 'err'
      llmLabel.value = 'LLM offline'
    }
  } catch {
    llmStatus.value = 'err'
    llmLabel.value = 'LLM offline'
  }
})
</script>

<style scoped>
.layout { display: flex; height: 100vh; overflow: hidden; }

.sidebar {
  width: 200px;
  flex-shrink: 0;
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  padding: 16px 12px;
  gap: 4px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 8px 16px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 8px;
}
.brand-icon { font-size: 20px; }
.brand-name { font-weight: 600; font-size: 15px; }

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: var(--radius);
  color: var(--text-muted);
  transition: background 0.15s, color 0.15s;
  font-size: 14px;
}
.nav-item:hover { background: var(--surface2); color: var(--text); }
.nav-item.router-link-active { background: var(--surface2); color: var(--accent); }

.sidebar-footer { margin-top: auto; }

.llm-status {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--text-muted);
  border-radius: var(--radius);
  background: var(--surface2);
}
.dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--text-muted);
  flex-shrink: 0;
}
.llm-status.ok .dot { background: var(--success); }
.llm-status.err .dot { background: var(--danger); }

.content {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
</style>
