<template>
  <div class="publish-page">
    <!-- 页面标题区 -->
    <div class="header">
      <div class="header-left">
        <h2>📡 接口发布</h2>
        <p class="header-desc muted">管理对外发布的 API 端点，配置技能与访问密钥</p>
      </div>
      <button class="btn-primary" @click="showCreate = true">+ 新建发布</button>
    </div>

    <!-- 加载状态 -->
    <div v-if="loading" class="loading-state">
      <div class="loading-spinner"></div>
      <span class="muted">正在加载发布配置…</span>
    </div>

    <!-- 空状态 -->
    <div v-else-if="configs.length === 0" class="empty-state">
      <div class="empty-icon">🚀</div>
      <p class="empty-title">还没有发布配置</p>
      <p class="muted">点击「新建发布」创建第一个对外接口，让你的技能通过 API 对外服务</p>
      <button class="btn-primary" @click="showCreate = true" style="margin-top: 16px;">+ 新建发布</button>
    </div>

    <!-- 配置卡片网格 -->
    <div class="configs-grid" v-else>
      <div v-for="config in configs" :key="config.endpoint_id" class="config-card">
        <div class="card-header">
          <div class="card-title-area">
            <span class="model-name">{{ config.name }}</span>
            <span :class="['status-badge', config.is_active ? 'active' : 'inactive']">
              {{ config.is_active ? '● 已启用' : '○ 已停用' }}
            </span>
          </div>
          <div class="switch-area">
            <span class="switch-label muted">{{ config.is_active ? '启用' : '停用' }}</span>
            <label class="switch">
              <input type="checkbox" :checked="config.is_active" @change="onToggle(config)" />
              <span class="slider"></span>
            </label>
          </div>
        </div>

        <div class="card-body">
          <!-- 技能区 -->
          <div class="section-label">🧩 已启用技能（{{ config.enabled_skills?.length || 0 }}）</div>
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
            <div v-if="availableSkills.length === 0" class="muted" style="padding: 8px 0;">暂无可用技能</div>
          </div>

          <!-- API 端点 -->
          <div class="section-label">🔗 API 端点</div>
          <div class="endpoint-info">
            <code class="code-block">POST {{ baseUrl }}/published/v1/chat/completions</code>
            <button class="btn-ghost btn-sm" @click="copyUrl(config)">
              {{ copyFeedback === 'url-' + config.endpoint_id ? '✓ 已复制' : '📋 复制' }}
            </button>
          </div>

          <!-- API Key -->
          <div class="section-label">🔑 API Key</div>
          <div class="key-info">
            <code class="code-block">{{ maskKey(config.api_key) }}</code>
            <button class="btn-ghost btn-sm" @click="copyKey(config)">
              {{ copyFeedback === 'key-' + config.endpoint_id ? '✓ 已复制' : '📋 复制' }}
            </button>
            <button class="btn-ghost btn-sm" @click="onRegenerateKey(config)">🔄 重新生成</button>
          </div>

          <!-- 调用示例 -->
          <div class="section-label">📝 调用示例</div>
          <pre class="curl-example">{{ curlExample(config) }}</pre>
        </div>

        <div class="card-footer">
          <button class="btn-danger" @click="onDelete(config)">🗑️ 删除此配置</button>
        </div>
      </div>
    </div>

    <!-- 新建发布弹窗 -->
    <div v-if="showCreate" class="modal-overlay" @click.self="showCreate = false">
      <div class="modal">
        <h3>✨ 新建发布</h3>
        <p class="modal-desc muted">创建一个新的 API 端点，选择要对外提供的技能</p>
        <div class="form-group">
          <label>模型名称</label>
          <input v-model="newName" placeholder="输入模型名称，如 my-skilled-model" />
          <span class="form-hint muted">用于调用时 model 参数的名称标识</span>
        </div>
        <div class="form-group">
          <label>选择要启用的技能</label>
          <div class="skill-check-list">
            <div v-for="skill in availableSkills" :key="skill.name" class="skill-check">
              <label>
                <input type="checkbox" :value="skill.name" v-model="newSkills" />
                <span>{{ skill.display_name || skill.name }}</span>
              </label>
            </div>
            <div v-if="availableSkills.length === 0" class="muted" style="padding: 8px 0;">暂无可用技能，请先创建技能</div>
          </div>
        </div>
        <div class="modal-actions">
          <button class="btn-primary" @click="onCreate" :disabled="!newName">确认创建</button>
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
const copyFeedback = ref('')

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
  if (confirm(`确定要删除「${config.name}」吗？删除后将无法恢复。`)) {
    await deleteConfig(config.endpoint_id)
  }
}

async function onRegenerateKey(config) {
  if (confirm('确定要重新生成 API Key 吗？旧 Key 将立即失效，使用旧 Key 的客户端将无法访问。')) {
    await regenerateKey(config.endpoint_id)
  }
}

function maskKey(key) {
  if (!key) return '***'
  return key.slice(0, 10) + '...' + key.slice(-4)
}

function showCopySuccess(id) {
  copyFeedback.value = id
  setTimeout(() => {
    if (copyFeedback.value === id) copyFeedback.value = ''
  }, 2000)
}

function copyUrl(config) {
  navigator.clipboard.writeText(`${baseUrl.value}/published/v1/chat/completions`)
  showCopySuccess('url-' + config.endpoint_id)
}

function copyKey(config) {
  navigator.clipboard.writeText(config.api_key || '')
  showCopySuccess('key-' + config.endpoint_id)
}

function curlExample(config) {
  return `# 调用示例 - 将 YOUR_API_KEY 替换为实际的 API Key
curl ${baseUrl.value}/published/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: ******" \\
  -d '{
    "model": "${config.name}",
    "messages": [{"role": "user", "content": "你好"}]
  }'`
}
</script>

<style scoped>
.publish-page {
  padding: 24px 32px;
  max-width: 1200px;
  margin: 0 auto;
  height: 100%;
  overflow-y: auto;
}

/* 页面标题 */
.header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 28px;
}

.header-left h2 {
  font-size: 22px;
  font-weight: 700;
  margin-bottom: 4px;
}

.header-desc {
  font-size: 13px;
}

/* 加载状态 */
.loading-state {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 32px 16px;
}

.loading-spinner {
  width: 20px;
  height: 20px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* 空状态 */
.empty-state {
  text-align: center;
  padding: 60px 20px;
  background: var(--surface);
  border: 1px dashed var(--border);
  border-radius: 12px;
}

.empty-icon {
  font-size: 48px;
  margin-bottom: 12px;
}

.empty-title {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 8px;
}

/* 卡片网格 */
.configs-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
  gap: 20px;
}

.config-card {
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
  background: var(--surface);
  transition: border-color 0.2s, box-shadow 0.2s;
}

.config-card:hover {
  border-color: var(--accent);
  box-shadow: 0 4px 20px rgba(108, 138, 255, 0.08);
}

/* 卡片头部 */
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border);
}

.card-title-area {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.model-name {
  font-size: 17px;
  font-weight: 600;
}

.status-badge {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 10px;
  width: fit-content;
}

.status-badge.active {
  color: var(--success);
  background: rgba(76, 175, 130, 0.12);
}

.status-badge.inactive {
  color: var(--text-muted);
  background: rgba(122, 128, 153, 0.12);
}

.switch-area {
  display: flex;
  align-items: center;
  gap: 8px;
}

.switch-label {
  font-size: 12px;
}

/* 卡片主体 */
.card-body {
  font-size: 14px;
}

.section-label {
  font-weight: 500;
  margin-top: 16px;
  margin-bottom: 8px;
  color: var(--text);
  font-size: 13px;
  padding-bottom: 4px;
  border-bottom: 1px solid var(--border);
}

/* 技能列表 */
.skills-list {
  max-height: 180px;
  overflow-y: auto;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px 12px;
}

.skill-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
}

.skill-toggle + .skill-toggle {
  border-top: 1px solid var(--border);
}

.skill-name {
  font-weight: 500;
  font-size: 13px;
}

.skill-desc {
  font-size: 12px;
}

/* 端点/密钥信息 */
.endpoint-info, .key-info {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.code-block {
  background: var(--surface2);
  border: 1px solid var(--border);
  padding: 6px 10px;
  border-radius: var(--radius);
  font-size: 12px;
  word-break: break-all;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  color: var(--text);
}

/* curl 示例 */
.curl-example {
  background: var(--surface2);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 14px;
  border-radius: var(--radius);
  font-size: 12px;
  overflow-x: auto;
  white-space: pre-wrap;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  line-height: 1.5;
}

/* 卡片底部 */
.card-footer {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
  display: flex;
  justify-content: flex-end;
}

/* 开关样式 */
.switch {
  position: relative;
  display: inline-block;
  width: 44px;
  height: 24px;
  flex-shrink: 0;
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
  background-color: var(--border);
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
  background-color: var(--text);
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
  background-color: var(--accent);
}

input:checked + .slider::before {
  transform: translateX(20px);
}

.switch.small input:checked + .slider::before {
  transform: translateX(16px);
}

/* 弹窗 */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  backdrop-filter: blur(4px);
}

.modal {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 28px;
  min-width: 400px;
  max-width: 500px;
  max-height: 80vh;
  overflow-y: auto;
}

.modal h3 {
  font-size: 18px;
  margin-bottom: 4px;
}

.modal-desc {
  font-size: 13px;
  margin-bottom: 20px;
}

.form-group {
  margin-bottom: 18px;
}

.form-group label {
  display: block;
  font-weight: 500;
  margin-bottom: 6px;
  font-size: 13px;
}

.form-hint {
  display: block;
  font-size: 12px;
  margin-top: 4px;
}

.skill-check-list {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px 12px;
  max-height: 180px;
  overflow-y: auto;
}

.skill-check {
  padding: 6px 0;
}

.skill-check + .skill-check {
  border-top: 1px solid var(--border);
}

.skill-check label {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  font-weight: 400;
}

.skill-check input[type="checkbox"] {
  width: auto;
  flex-shrink: 0;
}

.modal-actions {
  display: flex;
  gap: 8px;
  margin-top: 20px;
  justify-content: flex-end;
}

/* 按钮尺寸 */
.btn-sm {
  padding: 4px 10px;
  font-size: 12px;
}

/* 响应式 */
@media (max-width: 768px) {
  .publish-page {
    padding: 16px;
  }

  .header {
    flex-direction: column;
    gap: 12px;
  }

  .configs-grid {
    grid-template-columns: 1fr;
  }

  .modal {
    min-width: unset;
    width: calc(100vw - 32px);
    margin: 16px;
  }

  .endpoint-info, .key-info {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
