<template>
  <div class="publish-page">
    <div class="header">
      <h2>接口发布</h2>
      <button class="btn-primary" @click="showCreate = true">+ 新建发布</button>
    </div>

    <div v-if="loading" class="muted p16">加载中…</div>
    <div v-else-if="configs.length === 0" class="muted p16">
      还没有发布配置。点击「新建发布」创建第一个对外接口。
    </div>

    <div class="configs-grid" v-else>
      <div v-for="config in configs" :key="config.endpoint_id" class="config-card">
        <div class="card-header">
          <span class="model-name">{{ config.name }}</span>
          <label class="switch">
            <input type="checkbox" :checked="config.is_active" @change="onToggle(config)" />
            <span class="slider"></span>
          </label>
        </div>

        <div class="card-body">
          <div class="section-label">已启用技能 ({{ config.enabled_skills?.length || 0 }})</div>
          <div class="skills-list">
            <div v-for="skill in availableSkills" :key="skill.name" class="skill-toggle">
              <label class="switch small">
                <input
                  type="checkbox"
                  :checked="config.enabled_skills?.includes(skill.name)"
                  @change="onSkillToggle(config, skill.name, $event)"
                />
                <span class="slider"></span>
              </label>
              <span class="skill-name">{{ skill.display_name || skill.name }}</span>
              <span class="skill-desc muted">{{ skill.description }}</span>
            </div>
            <div v-if="availableSkills.length === 0" class="muted">暂无可用技能</div>
          </div>

          <div class="section-label">API 端点</div>
          <div class="endpoint-info">
            <code class="endpoint-url">POST {{ baseUrl }}/published/v1/chat/completions</code>
            <button class="btn-ghost btn-sm" @click="copyUrl(config)">复制</button>
          </div>

          <div class="section-label">API Key</div>
          <div class="key-info">
            <code class="api-key">{{ maskKey(config.api_key) }}</code>
            <button class="btn-ghost btn-sm" @click="copyKey(config)">复制</button>
            <button class="btn-ghost btn-sm" @click="onRegenerateKey(config)">重新生成</button>
          </div>

          <div class="section-label">调用示例</div>
          <pre class="curl-example">{{ curlExample(config) }}</pre>
        </div>

        <div class="card-footer">
          <button class="btn-ghost btn-danger" @click="onDelete(config)">删除</button>
        </div>
      </div>
    </div>

    <!-- Create Dialog -->
    <div v-if="showCreate" class="modal-overlay" @click.self="showCreate = false">
      <div class="modal">
        <h3>新建发布</h3>
        <div class="form-group">
          <label>模型名称</label>
          <input v-model="newName" placeholder="my-skilled-model" />
        </div>
        <div class="form-group">
          <label>启用技能</label>
          <div v-for="skill in availableSkills" :key="skill.name" class="skill-check">
            <label>
              <input type="checkbox" :value="skill.name" v-model="newSkills" />
              {{ skill.display_name || skill.name }}
            </label>
          </div>
        </div>
        <div class="modal-actions">
          <button class="btn-primary" @click="onCreate" :disabled="!newName">创建</button>
          <button class="btn-ghost" @click="showCreate = false">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, computed } from 'vue'
import { usePublish } from '../composables/usePublish.js'

const {
  configs,
  availableSkills,
  loading,
  error,
  fetchConfigs,
  fetchAvailableSkills,
  createConfig,
  updateConfig,
  deleteConfig,
  toggleConfig,
  regenerateKey,
} = usePublish()

const showCreate = ref(false)
const newName = ref('')
const newSkills = ref([])

const baseUrl = computed(() => window.location.origin)

onMounted(async () => {
  await Promise.all([fetchConfigs(), fetchAvailableSkills()])
})

async function onToggle(config) {
  await toggleConfig(config.endpoint_id)
}

async function onSkillToggle(config, skillName, event) {
  const enabled = event.target.checked
  let skills = [...(config.enabled_skills || [])]
  if (enabled && !skills.includes(skillName)) {
    skills.push(skillName)
  } else if (!enabled) {
    skills = skills.filter(s => s !== skillName)
  }
  await updateConfig(config.endpoint_id, { enabled_skills: skills })
}

async function onCreate() {
  await createConfig({
    name: newName.value,
    enabled_skills: newSkills.value,
    is_active: false,
  })
  showCreate.value = false
  newName.value = ''
  newSkills.value = []
}

async function onDelete(config) {
  if (confirm(`确定删除「${config.name}」？`)) {
    await deleteConfig(config.endpoint_id)
  }
}

async function onRegenerateKey(config) {
  if (confirm('重新生成 API Key？旧 Key 将立即失效。')) {
    await regenerateKey(config.endpoint_id)
  }
}

function maskKey(key) {
  if (!key) return '***'
  return key.slice(0, 10) + '...' + key.slice(-4)
}

function copyUrl(config) {
  navigator.clipboard.writeText(`${baseUrl.value}/published/v1/chat/completions`)
}

function copyKey(config) {
  navigator.clipboard.writeText(config.api_key || '')
}

function curlExample(config) {
  return `curl ${baseUrl.value}/published/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: ****** || 'YOUR_API_KEY'}" \\
  -d '{
    "model": "${config.name}",
    "messages": [{"role": "user", "content": "Hello"}]
  }'`
}
</script>

<style scoped>
.publish-page {
  padding: 24px;
  max-width: 1200px;
  margin: 0 auto;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;
}

.configs-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(480px, 1fr));
  gap: 20px;
}

.config-card {
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  padding: 20px;
  background: #fff;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.model-name {
  font-size: 18px;
  font-weight: 600;
}

.card-body {
  font-size: 14px;
}

.section-label {
  font-weight: 500;
  margin-top: 12px;
  margin-bottom: 6px;
  color: #475569;
}

.skills-list {
  max-height: 200px;
  overflow-y: auto;
}

.skill-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
}

.skill-name {
  font-weight: 500;
}

.skill-desc {
  font-size: 12px;
}

.endpoint-info, .key-info {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.endpoint-url, .api-key {
  background: #f1f5f9;
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 12px;
  word-break: break-all;
}

.curl-example {
  background: #1e293b;
  color: #e2e8f0;
  padding: 12px;
  border-radius: 8px;
  font-size: 12px;
  overflow-x: auto;
  white-space: pre-wrap;
}

.card-footer {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid #e2e8f0;
}

/* Switch styles */
.switch {
  position: relative;
  display: inline-block;
  width: 44px;
  height: 24px;
}

.switch.small {
  width: 34px;
  height: 18px;
}

.switch input {
  opacity: 0;
  width: 0;
  height: 0;
}

.slider {
  position: absolute;
  cursor: pointer;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background-color: #cbd5e1;
  transition: 0.3s;
  border-radius: 24px;
}

.slider::before {
  position: absolute;
  content: "";
  height: 18px;
  width: 18px;
  left: 3px;
  bottom: 3px;
  background-color: white;
  transition: 0.3s;
  border-radius: 50%;
}

.switch.small .slider::before {
  height: 14px;
  width: 14px;
  left: 2px;
  bottom: 2px;
}

input:checked + .slider {
  background-color: #3b82f6;
}

input:checked + .slider::before {
  transform: translateX(20px);
}

.switch.small input:checked + .slider::before {
  transform: translateX(16px);
}

/* Modal */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.modal {
  background: white;
  border-radius: 12px;
  padding: 24px;
  min-width: 400px;
  max-width: 500px;
  max-height: 80vh;
  overflow-y: auto;
}

.form-group {
  margin-bottom: 16px;
}

.form-group label {
  display: block;
  font-weight: 500;
  margin-bottom: 4px;
}

.form-group input[type="text"],
.form-group input:not([type]) {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
}

.skill-check {
  padding: 4px 0;
}

.modal-actions {
  display: flex;
  gap: 8px;
  margin-top: 16px;
}

.btn-primary {
  background: #3b82f6;
  color: white;
  border: none;
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
}

.btn-primary:disabled {
  opacity: 0.5;
}

.btn-ghost {
  background: none;
  border: 1px solid #e2e8f0;
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
}

.btn-sm {
  padding: 4px 8px;
  font-size: 12px;
}

.btn-danger {
  color: #ef4444;
  border-color: #fecaca;
}

.muted {
  color: #64748b;
}

.p16 {
  padding: 16px;
}
</style>
