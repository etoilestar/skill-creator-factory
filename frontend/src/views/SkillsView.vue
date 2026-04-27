<template>
  <div class="skills-page">
    <div class="header">
      <h2>Skills 库</h2>
      <button class="btn-primary" @click="openNew">+ 新建 Skill</button>
    </div>

    <div class="body">
      <!-- Skill list -->
      <div class="list-panel">
        <div v-if="loading" class="muted p16">加载中…</div>
        <div v-else-if="skills.length === 0" class="muted p16">
          还没有 Skill。用 Creator 创建第一个吧！
        </div>
        <div
          v-for="sk in skills"
          :key="sk.name"
          class="skill-item"
          :class="{ active: selected?.name === sk.name }"
          @click="select(sk.name)"
        >
          <div class="sk-name">{{ sk.display_name || sk.name }}</div>
          <div class="sk-desc muted">{{ sk.description || '暂无描述' }}</div>
        </div>
      </div>

      <!-- Detail panel -->
      <div class="detail-panel">
        <div v-if="!selected && !editing" class="muted p16">← 选择一个 Skill 查看详情</div>

        <!-- Editor mode -->
        <template v-if="editing">
          <div class="detail-header">
            <input v-model="editName" placeholder="skill-name" style="max-width:220px" />
            <button class="btn-primary" @click="save" :disabled="saving">{{ saving ? '保存中…' : '保存' }}</button>
            <button class="btn-ghost" @click="cancelEdit">取消</button>
          </div>
          <div v-if="editError" class="error px16">{{ editError }}</div>
          <textarea v-model="editContent" class="skill-editor" spellcheck="false" />
        </template>

        <!-- View mode -->
        <template v-else-if="selected">
          <div class="detail-header">
            <span class="detail-title">{{ selected.display_name || selected.name }}</span>
            <button class="btn-ghost" @click="startEdit">编辑</button>
            <button class="btn-danger" @click="confirmDelete">删除</button>
          </div>
          <pre class="skill-preview">{{ selected.content }}</pre>
        </template>
      </div>
    </div>

    <!-- Delete confirm -->
    <div v-if="deleteTarget" class="overlay" @click.self="deleteTarget = null">
      <div class="dialog">
        <p>确认删除 <strong>{{ deleteTarget }}</strong>？此操作不可撤销。</p>
        <div class="dialog-actions">
          <button class="btn-danger" @click="doDelete">删除</button>
          <button class="btn-ghost" @click="deleteTarget = null">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { fetchSkills, fetchSkill, saveSkill, deleteSkill } from '../composables/useSkills.js'

const skills = ref([])
const selected = ref(null)
const loading = ref(true)
const editing = ref(false)
const editName = ref('')
const editContent = ref('')
const editError = ref('')
const saving = ref(false)
const deleteTarget = ref(null)

async function load() {
  loading.value = true
  skills.value = await fetchSkills()
  loading.value = false
}

async function select(name) {
  editing.value = false
  selected.value = await fetchSkill(name)
}

function openNew() {
  selected.value = null
  editing.value = true
  editName.value = ''
  editContent.value = `---\nname: my-skill\ndescription: Describe what this skill does and when to use it.\n---\n\n# My Skill\n\n`
  editError.value = ''
}

function startEdit() {
  editName.value = selected.value.name
  editContent.value = selected.value.content
  editError.value = ''
  editing.value = true
}

function cancelEdit() {
  editing.value = false
  editError.value = ''
}

async function save() {
  const name = editName.value.trim()
  if (!name) { editError.value = 'Skill 名称不能为空'; return }
  saving.value = true
  editError.value = ''
  try {
    await saveSkill(name, editContent.value)
    await load()
    editing.value = false
    selected.value = await fetchSkill(name)
  } catch (e) {
    editError.value = e.message
  } finally {
    saving.value = false
  }
}

function confirmDelete() {
  deleteTarget.value = selected.value.name
}

async function doDelete() {
  const name = deleteTarget.value
  deleteTarget.value = null
  await deleteSkill(name)
  selected.value = null
  await load()
}

onMounted(load)
</script>

<style scoped>
.skills-page { display: flex; flex-direction: column; height: 100%; overflow: hidden; }

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px 24px 12px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.header h2 { font-size: 18px; font-weight: 600; }

.body { display: flex; flex: 1; overflow: hidden; }

.list-panel {
  width: 240px;
  flex-shrink: 0;
  border-right: 1px solid var(--border);
  overflow-y: auto;
}

.skill-item {
  padding: 14px 16px;
  cursor: pointer;
  border-bottom: 1px solid var(--border);
  transition: background 0.15s;
}
.skill-item:hover { background: var(--surface2); }
.skill-item.active { background: var(--surface2); border-left: 3px solid var(--accent); }

.sk-name { font-weight: 500; margin-bottom: 4px; }
.sk-desc { font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.detail-panel { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

.detail-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.detail-title { font-weight: 600; flex: 1; }

.skill-preview {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  font-family: 'Fira Code', 'Cascadia Code', monospace;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--bg);
  margin: 0;
}

.skill-editor {
  flex: 1;
  border: none;
  border-radius: 0;
  font-family: 'Fira Code', 'Cascadia Code', monospace;
  font-size: 13px;
  background: var(--bg);
  padding: 16px;
  resize: none;
}

.p16 { padding: 16px; }
.px16 { padding: 4px 16px; }

.overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.6);
  display: flex; align-items: center; justify-content: center;
  z-index: 100;
}
.dialog {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 24px;
  max-width: 380px;
  width: 90%;
}
.dialog p { margin-bottom: 20px; }
.dialog-actions { display: flex; gap: 10px; }
</style>
