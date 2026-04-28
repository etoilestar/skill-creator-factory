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

          <!-- Asset files section -->
          <div class="assets-section">
            <div class="assets-header">资产文件</div>

            <!-- File groups -->
            <div v-for="folder in assetFolders" :key="folder" class="asset-group">
              <div class="asset-group-title">{{ folder }}</div>
              <div v-if="assets[folder] && assets[folder].length" class="asset-list">
                <div v-for="fname in assets[folder]" :key="fname" class="asset-row">
                  <span class="asset-name">{{ fname }}</span>
                  <button class="btn-icon-edit" :aria-label="`编辑 ${fname}`" :title="`编辑 ${fname}`" @click="openAssetEditor(folder, fname)">✎</button>
                  <button class="btn-icon-danger" :aria-label="`删除 ${fname}`" :title="`删除 ${fname}`" @click="removeAsset(folder, fname)">✕</button>
                </div>
              </div>
              <div v-else class="muted asset-empty">暂无文件</div>
            </div>

            <!-- Upload row -->
            <div class="upload-row">
              <select v-model="uploadFolder" class="folder-select" aria-label="上传目录">
                <option v-for="f in assetFolders" :key="f" :value="f">{{ f }}</option>
              </select>
              <label class="file-input-label">
                <input ref="fileInputRef" type="file" class="file-input" aria-label="选择要上传的文件" @change="onFileChange" />
              </label>
              <button class="btn-primary" :disabled="!uploadFile || uploading" @click="doUpload">
                {{ uploading ? '上传中…' : '上传' }}
              </button>
            </div>
            <div v-if="uploadError" class="error px16">{{ uploadError }}</div>
            <div v-if="assetError" class="error px16">{{ assetError }}</div>
          </div>
        </template>
      </div>
    </div>

    <!-- Asset editor modal -->
  <div v-if="assetEditor" class="overlay" @click.self="closeAssetEditor">
    <div class="dialog dialog-editor">
      <div class="dialog-title">{{ assetEditor.folder }}/{{ assetEditor.filename }}</div>
      <div v-if="assetEditor.loading" class="muted p16">加载中…</div>
      <div v-else-if="assetEditor.binary" class="muted p16">该文件为二进制，无法编辑。</div>
      <textarea
        v-else
        v-model="assetEditor.content"
        class="skill-editor asset-edit-textarea"
        spellcheck="false"
      />
      <div v-if="assetEditor.error" class="error px16">{{ assetEditor.error }}</div>
      <div class="dialog-actions">
        <template v-if="!assetEditor.binary">
          <button class="btn-primary" :disabled="assetEditor.saving" @click="saveAssetEdit">
            {{ assetEditor.saving ? '保存中…' : '保存' }}
          </button>
        </template>
        <button class="btn-ghost" @click="closeAssetEditor">{{ assetEditor.binary ? '关闭' : '取消' }}</button>
      </div>
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
import { fetchSkills, fetchSkill, saveSkill, deleteSkill, fetchSkillAssets, uploadAsset, deleteAsset, fetchAssetContent, saveAssetContent } from '../composables/useSkills.js'

const skills = ref([])
const selected = ref(null)
const loading = ref(true)
const editing = ref(false)
const editName = ref('')
const editContent = ref('')
const editError = ref('')
const saving = ref(false)
const deleteTarget = ref(null)

// assets
const assetFolders = ['assets', 'references', 'scripts']
const assets = ref({ assets: [], references: [], scripts: [] })
const uploadFolder = ref('assets')
const uploadFile = ref(null)
const uploading = ref(false)
const uploadError = ref('')
const assetError = ref('')
const fileInputRef = ref(null)

// asset editor
const assetEditor = ref(null)

async function load() {
  loading.value = true
  skills.value = await fetchSkills()
  loading.value = false
}

async function loadAssets(name) {
  try {
    assets.value = await fetchSkillAssets(name)
  } catch {
    assets.value = { assets: [], references: [], scripts: [] }
  }
}

async function select(name) {
  editing.value = false
  selected.value = await fetchSkill(name)
  uploadError.value = ''
  assetError.value = ''
  await loadAssets(name)
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
    await loadAssets(name)
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
  assets.value = { assets: [], references: [], scripts: [] }
  uploadError.value = ''
  assetError.value = ''
  await load()
}

function onFileChange(e) {
  uploadFile.value = e.target.files[0] || null
  uploadError.value = ''
}

async function doUpload() {
  if (!uploadFile.value || !selected.value) return
  uploading.value = true
  uploadError.value = ''
  try {
    await uploadAsset(selected.value.name, uploadFolder.value, uploadFile.value)
    uploadFile.value = null
    if (fileInputRef.value) fileInputRef.value.value = ''
    await loadAssets(selected.value.name)
  } catch (e) {
    uploadError.value = e.message
  } finally {
    uploading.value = false
  }
}

async function removeAsset(folder, filename) {
  if (!selected.value) return
  assetError.value = ''
  try {
    await deleteAsset(selected.value.name, folder, filename)
    await loadAssets(selected.value.name)
  } catch (e) {
    assetError.value = e.message
  }
}

async function openAssetEditor(folder, filename) {
  assetEditor.value = { folder, filename, content: '', loading: true, binary: false, saving: false, error: '' }
  try {
    const text = await fetchAssetContent(selected.value.name, folder, filename)
    assetEditor.value = { folder, filename, content: text, loading: false, binary: false, saving: false, error: '' }
  } catch (e) {
    if (e.status === 415) {
      assetEditor.value = { folder, filename, content: '', loading: false, binary: true, saving: false, error: '' }
    } else {
      assetEditor.value = null
      assetError.value = e.message
    }
  }
}

function closeAssetEditor() {
  assetEditor.value = null
}

async function saveAssetEdit() {
  if (!assetEditor.value || !selected.value) return
  assetEditor.value.saving = true
  assetEditor.value.error = ''
  try {
    await saveAssetContent(selected.value.name, assetEditor.value.folder, assetEditor.value.filename, assetEditor.value.content)
    assetEditor.value = null
    await loadAssets(selected.value.name)
  } catch (e) {
    assetEditor.value.error = e.message
  } finally {
    if (assetEditor.value) assetEditor.value.saving = false
  }
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
  min-height: 0;
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

/* Asset files section */
.assets-section {
  border-top: 1px solid var(--border);
  flex-shrink: 0;
  padding: 12px 16px 16px;
  overflow-y: auto;
  max-height: 300px;
}
.assets-header {
  font-weight: 600;
  font-size: 13px;
  margin-bottom: 10px;
}
.asset-group { margin-bottom: 10px; }
.asset-group-title {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted, #888);
  margin-bottom: 4px;
}
.asset-list { display: flex; flex-direction: column; gap: 2px; }
.asset-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 6px;
  border-radius: 4px;
  background: var(--surface2);
}
.asset-name { flex: 1; font-size: 12px; font-family: 'Fira Code', monospace; word-break: break-all; }
.asset-empty { font-size: 12px; padding: 2px 0; }
.btn-icon-danger {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--danger, #e55);
  padding: 0 4px;
  font-size: 11px;
  line-height: 1;
  border-radius: 3px;
  flex-shrink: 0;
}
.btn-icon-danger:hover { background: var(--danger-bg, rgba(220,50,50,0.12)); }
.upload-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}
.folder-select {
  font-size: 12px;
  padding: 4px 6px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: inherit;
}
.file-input { flex: 1; font-size: 12px; min-width: 0; }

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
.dialog-actions { display: flex; gap: 10px; margin-top: 12px; }
.dialog-title { font-weight: 600; font-size: 14px; margin-bottom: 12px; font-family: 'Fira Code', monospace; }
.dialog-editor { width: 680px; max-width: 94vw; display: flex; flex-direction: column; }
.asset-edit-textarea { flex: 1; min-height: 340px; resize: vertical; border: 1px solid var(--border); border-radius: 6px; margin-bottom: 4px; }
.btn-icon-edit {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--accent, #4a9);
  padding: 0 4px;
  font-size: 13px;
  line-height: 1;
  border-radius: 3px;
  flex-shrink: 0;
}
.btn-icon-edit:hover { background: var(--surface2); }
</style>
