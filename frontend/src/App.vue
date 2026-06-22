<template>
  <div class="layout">
    <nav class="sidebar" :class="{ collapsed: sideCollapsed }">
      <div class="brand">
        <span class="brand-icon">⚡</span>
        <span v-if="!sideCollapsed" class="brand-name">技能工厂</span>
      </div>
      <RouterLink to="/creator" class="nav-item">
        <span class="nav-icon">🛠</span>
        <span v-if="!sideCollapsed">技能创建</span>
      </RouterLink>
      <RouterLink to="/creator/tools" class="nav-item">
        <span class="nav-icon">🧰</span>
        <span v-if="!sideCollapsed">工具管理</span>
      </RouterLink>
      <RouterLink to="/skills" class="nav-item">
        <span class="nav-icon">📚</span>
        <span v-if="!sideCollapsed">技能库</span>
      </RouterLink>
      <RouterLink to="/sandbox" class="nav-item">
        <span class="nav-icon">🧪</span>
        <span v-if="!sideCollapsed">沙盒测试</span>
      </RouterLink>
      <RouterLink to="/publish" class="nav-item">
        <span class="nav-icon">🚀</span>
        <span v-if="!sideCollapsed">端口发布</span>
      </RouterLink>
      <div class="sidebar-footer">
        <div v-if="!sideCollapsed" class="llm-status" :class="llmStatus">
          <span class="dot"></span>
          {{ llmLabel }}
        </div>
      </div>
      <button class="collapse-btn" @click="sideCollapsed = !sideCollapsed" :title="sideCollapsed ? '展开导航' : '折叠导航'">
        <span class="collapse-icon" :class="{ flipped: !sideCollapsed }">◀</span>
      </button>
    </nav>
    <main class="content">
      <RouterView />
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { fetchLlmHealth } from './composables/useSkills.js'

const sideCollapsed = ref(false)
const llmStatus = ref('unknown')
const llmLabel = ref('正在检查 LLM…')

onMounted(async () => {
  try {
    const data = await fetchLlmHealth()
    if (data.connected) {
      llmStatus.value = 'ok'
      llmLabel.value = `LLM 已连接`
    } else {
      llmStatus.value = 'err'
      llmLabel.value = 'LLM 离线'
    }
  } catch {
    llmStatus.value = 'err'
    llmLabel.value = 'LLM 离线'
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
  transition: width 0.25s ease, padding 0.25s ease;
  position: relative;
}
.sidebar.collapsed {
  width: 52px;
  padding: 12px 4px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 8px 16px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 8px;
  overflow: hidden;
}
.sidebar.collapsed .brand { padding: 8px 4px 16px; justify-content: center; }
.brand-icon { font-size: 20px; }
.brand-name { font-weight: 600; font-size: 15px; }

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: var(--radius);
  color: var(--text-muted);
  transition: background 0.15s, color 0.15s, padding 0.25s ease;
  font-size: 14px;
}
.sidebar.collapsed .nav-item {
  justify-content: center;
  padding: 10px 4px;
  gap: 0;
}
.nav-icon { font-size: 16px; flex-shrink: 0; }
.nav-item:hover { background: var(--surface2); color: var(--text); }
.nav-item.router-link-active { background: var(--surface2); color: var(--accent); }

.sidebar-footer { margin-top: auto; padding: 4px; }
.sidebar.collapsed .sidebar-footer { display: none; }

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

.collapse-btn {
  position: absolute;
  right: -12px;
  top: 24px;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text-muted);
  font-size: 11px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10;
  transition: transform 0.15s, background 0.15s, color 0.15s;
}
.collapse-btn:hover {
  background: var(--accent);
  color: white;
  border-color: var(--accent);
}
.collapse-icon {
  display: inline-block;
  transition: transform 0.25s ease;
}
.collapse-icon.flipped { transform: rotate(180deg); }

.content {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
</style>
