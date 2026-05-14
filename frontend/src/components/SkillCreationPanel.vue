<template>
  <div class="skill-creation-panel">
    <!-- Header -->
    <div class="panel-header">
      <span class="panel-icon">🛠</span>
      <span class="panel-title">
        创建 Skill：
        <input
          v-if="editingName"
          v-model="localSkillName"
          class="name-input"
          @blur="commitName"
          @keyup.enter="commitName"
          ref="nameInputRef"
          :class="{ error: nameError }"
        />
        <span v-else class="skill-name-display" @click="startEditName" title="点击修改名称">
          {{ localSkillName }}
          <span class="edit-hint">✏️</span>
        </span>
      </span>
      <span v-if="nameError" class="name-error">{{ nameError }}</span>
    </div>

    <!-- Progress bar -->
    <div class="progress-bar-wrap">
      <div
        class="progress-bar-fill"
        :style="{ width: progressPercent + '%' }"
      />
      <span class="progress-label">{{ doneCount }}/{{ localFiles.length }} 文件已完成</span>
    </div>

    <!-- Warnings from blueprint parser -->
    <div v-if="warnings.length" class="warnings">
      <div v-for="(w, i) in warnings" :key="i" class="warning-item">⚠️ {{ w }}</div>
    </div>

    <!-- File list -->
    <ul class="file-list">
      <li
        v-for="(file, idx) in localFiles"
        :key="file.path"
        class="file-item"
        :class="file.status"
      >
        <!-- Status icon -->
        <span class="file-icon">{{ statusIcon(file.status) }}</span>

        <!-- Path & meta -->
        <span class="file-path">{{ file.path }}</span>
        <span v-if="file.status === 'done'" class="file-meta">
          已写入 ({{ formatBytes(file.bytesWritten) }})
        </span>
        <span v-else-if="file.status === 'generating'" class="file-meta generating">
          生成中…
        </span>
        <span v-else-if="file.status === 'writing'" class="file-meta">写入中…</span>
        <span v-else-if="file.status === 'error'" class="file-meta error">{{ file.error }}</span>
        <span v-else-if="file.can_skip && file.status === 'pending'" class="file-meta muted">
          可跳过
        </span>

        <!-- Action buttons -->
        <span class="file-actions">
          <button
            v-if="file.status === 'preview'"
            class="btn-small btn-write"
            @click="writeOneFile(idx)"
          >
            写入
          </button>
          <button
            v-if="file.status === 'error' || file.status === 'preview'"
            class="btn-small btn-retry"
            @click="generateOneFile(idx)"
          >
            重新生成
          </button>
          <button
            v-if="file.status === 'done'"
            class="btn-small btn-preview"
            @click="togglePreview(idx)"
          >
            {{ file.showPreview ? '收起' : '预览' }}
          </button>
          <button
            v-if="canSkipFile(file)"
            class="btn-small btn-skip"
            @click="skipFile(idx)"
          >
            跳过
          </button>
        </span>

        <!-- Inline editor for preview/error states -->
        <div v-if="file.status === 'preview' || (file.status === 'error' && file.generatedContent)"
          class="file-editor-wrap"
        >
          <textarea
            v-model="file.generatedContent"
            class="file-editor"
            rows="12"
            :placeholder="'在此编辑 ' + file.path + ' 的内容…'"
          />
        </div>

        <!-- Collapsible preview for done files -->
        <div v-if="file.status === 'done' && file.showPreview" class="file-preview-wrap">
          <pre class="file-preview">{{ file.generatedContent }}</pre>
        </div>
      </li>

      <!-- Add file row -->
      <li class="file-item add-file-row">
        <button class="btn-add-file" @click="addFilePrompt = true">＋ 添加文件</button>
        <div v-if="addFilePrompt" class="add-file-form">
          <input
            v-model="newFilePath"
            class="add-file-input"
            placeholder="如：scripts/helper.py"
            @keyup.enter="addFile"
          />
          <input
            v-model="newFilePurpose"
            class="add-file-input"
            placeholder="职责说明（可选）"
            @keyup.enter="addFile"
          />
          <button class="btn-small btn-write" @click="addFile">确认添加</button>
          <button class="btn-small" @click="addFilePrompt = false">取消</button>
        </div>
      </li>
    </ul>

    <!-- Main action buttons -->
    <div class="panel-actions">
      <button
        v-if="phase === 'idle'"
        class="btn-primary"
        @click="startCreation"
        :disabled="localFiles.length === 0"
      >
        开始创建
      </button>
      <button
        v-if="phase === 'running'"
        class="btn-secondary"
        @click="pauseCreation"
      >
        暂停
      </button>
      <button
        v-if="phase === 'paused'"
        class="btn-primary"
        @click="resumeCreation"
      >
        继续
      </button>
      <button
        v-if="phase === 'running' || phase === 'paused'"
        class="btn-secondary"
        @click="skipOptionalFiles"
      >
        跳过所有可选文件
      </button>
    </div>

    <!-- Post-creation status -->
    <div v-if="phase === 'validating' || phase === 'packaging' || phase === 'complete'" class="post-status">
      <div class="post-item" :class="{ success: validateResult?.success, fail: validateResult && !validateResult.success }">
        <span>{{ validateResult ? (validateResult.success ? '✅ 校验通过' : '❌ 校验失败') : '⏳ 校验中…' }}</span>
        <span v-if="validateResult && !validateResult.success" class="post-detail">{{ validateResult.message }}</span>
      </div>
      <div v-if="packageResult !== null" class="post-item" :class="{ success: packageResult?.success }">
        <span>{{ packageResult.success ? '✅ 打包完成' : '❌ 打包失败' }}</span>
        <span v-if="packageResult && !packageResult.success" class="post-detail">{{ packageResult.message }}</span>
      </div>
    </div>

    <!-- Complete actions -->
    <div v-if="phase === 'complete'" class="complete-actions">
      <button class="btn-primary" @click="openInSandbox">在沙盒中打开</button>
      <a
        v-if="packageResult?.path"
        :href="packageDownloadUrl"
        class="btn-secondary"
        download
      >
        下载 .skill 包
      </a>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick } from 'vue'
import {
  initSkill,
  generateFileStream,
  writeFile,
  validateSkill,
  packageSkill,
} from '../composables/useCreator.js'

// ---------------------------------------------------------------------------
// Props & emits
// ---------------------------------------------------------------------------

const props = defineProps({
  skillName: { type: String, required: true },
  files: { type: Array, required: true },    // [{ path, purpose, required, can_skip }]
  blueprintText: { type: String, default: '' },
  conversationHistory: { type: Array, default: () => [] },
  model: { type: String, default: null },
  warnings: { type: Array, default: () => [] },
})

const emit = defineEmits(['creation-complete', 'creation-error'])

// ---------------------------------------------------------------------------
// Reactive state
// ---------------------------------------------------------------------------

// File status: 'pending' | 'generating' | 'preview' | 'writing' | 'done' | 'skipped' | 'error'
const localFiles = ref(
  props.files.map(f => ({
    ...f,
    status: 'pending',
    generatedContent: '',
    bytesWritten: 0,
    error: '',
    showPreview: false,
  }))
)

const localSkillName = ref(props.skillName)
const editingName = ref(false)
const nameError = ref('')
const nameInputRef = ref(null)

const phase = ref('idle')   // idle | running | paused | validating | packaging | complete
const paused = ref(false)

const validateResult = ref(null)
const packageResult = ref(null)

const addFilePrompt = ref(false)
const newFilePath = ref('')
const newFilePurpose = ref('')

// ---------------------------------------------------------------------------
// Computed
// ---------------------------------------------------------------------------

const doneCount = computed(
  () => localFiles.value.filter(f => f.status === 'done').length
)

const progressPercent = computed(() => {
  const total = localFiles.value.filter(f => f.status !== 'skipped').length
  if (!total) return 0
  return Math.round((doneCount.value / total) * 100)
})

const packageDownloadUrl = computed(() => {
  if (!packageResult.value?.path) return '#'
  const name = localSkillName.value
  return `/api/skills/${name}/files/dist/${name}.skill`
})

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function statusIcon(status) {
  const icons = {
    pending:    '⬜',
    generating: '⟳',
    preview:    '👁',
    writing:    '✏️',
    done:       '✅',
    skipped:    '⏭',
    error:      '❌',
  }
  return icons[status] ?? '⬜'
}

function formatBytes(n) {
  if (!n) return '0 B'
  if (n < 1024) return `${n} B`
  return `${(n / 1024).toFixed(1)} KB`
}

function canSkipFile(file) {
  return (
    file.can_skip &&
    file.status !== 'done' &&
    file.status !== 'skipped' &&
    file.status !== 'writing'
  )
}

// ---------------------------------------------------------------------------
// Name editing
// ---------------------------------------------------------------------------

function startEditName() {
  editingName.value = true
  nextTick(() => nameInputRef.value?.focus())
}

function commitName() {
  const raw = localSkillName.value.trim()
  if (!raw) {
    nameError.value = '名称不能为空'
    return
  }
  if (!/^[a-z0-9][a-z0-9\-]*$/.test(raw)) {
    nameError.value = '只能包含小写字母、数字和连字符'
    return
  }
  nameError.value = ''
  localSkillName.value = raw
  editingName.value = false
}

// ---------------------------------------------------------------------------
// File management
// ---------------------------------------------------------------------------

function addFile() {
  const path = newFilePath.value.trim()
  if (!path) return
  const allowed = ['SKILL.md', 'scripts/', 'references/', 'assets/']
  if (!allowed.some(p => path === p || path.startsWith(p))) {
    alert('路径必须是 SKILL.md 或 scripts/*、references/*、assets/* 下的文件')
    return
  }
  if (localFiles.value.some(f => f.path === path)) {
    alert('该文件路径已存在')
    return
  }
  localFiles.value.push({
    path,
    purpose: newFilePurpose.value.trim() || path,
    required: true,
    can_skip: false,
    status: 'pending',
    generatedContent: '',
    bytesWritten: 0,
    error: '',
    showPreview: false,
  })
  newFilePath.value = ''
  newFilePurpose.value = ''
  addFilePrompt.value = false
}

function skipFile(idx) {
  localFiles.value[idx].status = 'skipped'
}

function togglePreview(idx) {
  localFiles.value[idx].showPreview = !localFiles.value[idx].showPreview
}

// ---------------------------------------------------------------------------
// Per-file generation & writing
// ---------------------------------------------------------------------------

async function generateOneFile(idx) {
  const file = localFiles.value[idx]
  file.status = 'generating'
  file.error = ''
  file.generatedContent = ''

  try {
    for await (const chunk of generateFileStream({
      skillName: localSkillName.value,
      filePath: file.path,
      purpose: file.purpose,
      blueprintText: props.blueprintText,
      conversationHistory: props.conversationHistory,
      model: props.model,
    })) {
      if (typeof chunk === 'string') {
        file.generatedContent += chunk
      } else if (chunk?.done) {
        break
      } else if (chunk?.error) {
        throw new Error(chunk.error)
      }
    }

    if (!file.generatedContent.trim()) {
      throw new Error('模型未返回任何内容，请重试或手动填写')
    }

    file.status = 'preview'
  } catch (err) {
    file.status = 'error'
    file.error = err.message
  }
}

async function writeOneFile(idx) {
  const file = localFiles.value[idx]
  file.status = 'writing'
  try {
    const result = await writeFile(localSkillName.value, file.path, file.generatedContent)
    if (!result.success) throw new Error(result.message)
    file.status = 'done'
    file.bytesWritten = result.bytes || 0
  } catch (err) {
    file.status = 'error'
    file.error = err.message
  }
}

// ---------------------------------------------------------------------------
// Full creation flow
// ---------------------------------------------------------------------------

async function startCreation() {
  if (nameError.value) return
  phase.value = 'running'
  paused.value = false

  // 1. Init skill directory
  try {
    const r = await initSkill(localSkillName.value)
    if (!r.success) throw new Error(r.message)
  } catch (err) {
    emit('creation-error', err.message)
    phase.value = 'idle'
    return
  }

  // 2. Generate + write each file sequentially
  for (let idx = 0; idx < localFiles.value.length; idx++) {
    if (paused.value) {
      phase.value = 'paused'
      // Wait until resumed
      await new Promise(resolve => {
        const pauseCheckInterval = setInterval(() => {
          if (!paused.value) {
            clearInterval(pauseCheckInterval)
            resolve()
          }
        }, 200)
      })
      phase.value = 'running'
    }

    const file = localFiles.value[idx]
    if (file.status === 'skipped' || file.status === 'done') continue

    await generateOneFile(idx)

    // If still in error state after generation, stop if required
    if (localFiles.value[idx].status === 'error' && file.required) {
      phase.value = 'paused'
      return
    }
    if (localFiles.value[idx].status === 'error' && !file.required) {
      // skip optional file on error
      localFiles.value[idx].status = 'skipped'
      continue
    }

    await writeOneFile(idx)
    if (localFiles.value[idx].status === 'error' && file.required) {
      phase.value = 'paused'
      return
    }
  }

  // 3. Validate
  phase.value = 'validating'
  try {
    validateResult.value = await validateSkill(localSkillName.value)
  } catch (err) {
    validateResult.value = { success: false, message: err.message }
  }

  // 4. Package (even if validate failed — so users can still download)
  phase.value = 'packaging'
  try {
    packageResult.value = await packageSkill(localSkillName.value)
  } catch (err) {
    packageResult.value = { success: false, message: err.message, path: null }
  }

  phase.value = 'complete'
  emit('creation-complete', {
    skillName: localSkillName.value,
    validateResult: validateResult.value,
    packageResult: packageResult.value,
  })
}

function pauseCreation() {
  paused.value = true
}

function resumeCreation() {
  paused.value = false
}

function skipOptionalFiles() {
  localFiles.value.forEach(f => {
    if (f.can_skip && f.status === 'pending') {
      f.status = 'skipped'
    }
  })
}

function openInSandbox() {
  window.location.href = `/sandbox/${localSkillName.value}`
}
</script>

<style scoped>
.skill-creation-panel {
  border: 1px solid #3a3a3a;
  border-radius: 8px;
  padding: 16px;
  background: #1e1e1e;
  color: #d4d4d4;
  font-size: 14px;
  margin-top: 16px;
}

.panel-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.panel-icon { font-size: 18px; }

.skill-name-display {
  cursor: pointer;
  color: #80c8ff;
  text-decoration: underline dotted;
}
.edit-hint { font-size: 12px; margin-left: 4px; opacity: 0.6; }

.name-input {
  background: #2a2a2a;
  border: 1px solid #555;
  border-radius: 4px;
  color: #d4d4d4;
  padding: 2px 6px;
  font-size: 14px;
  width: 180px;
}
.name-input.error { border-color: #e05c5c; }
.name-error { color: #e05c5c; font-size: 12px; }

/* Progress */
.progress-bar-wrap {
  background: #2a2a2a;
  border-radius: 4px;
  height: 20px;
  position: relative;
  margin-bottom: 10px;
  overflow: hidden;
}
.progress-bar-fill {
  background: #3a7bd5;
  height: 100%;
  transition: width 0.3s ease;
}
.progress-label {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  text-align: center;
  line-height: 20px;
  font-size: 12px;
  color: #fff;
  pointer-events: none;
}

/* Warnings */
.warnings { margin-bottom: 10px; }
.warning-item {
  background: #3b2e00;
  border-radius: 4px;
  padding: 4px 8px;
  margin-bottom: 4px;
  font-size: 12px;
  color: #f0c060;
}

/* File list */
.file-list {
  list-style: none;
  margin: 0 0 12px 0;
  padding: 0;
}

.file-item {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-radius: 4px;
  margin-bottom: 4px;
  background: #252525;
}
.file-item.done     { background: #1a2a1a; }
.file-item.error    { background: #2a1a1a; }
.file-item.skipped  { opacity: 0.4; }

.file-icon { font-size: 14px; flex-shrink: 0; }
.file-path { font-family: monospace; flex: 1; }

.file-meta { font-size: 12px; color: #888; }
.file-meta.error { color: #e05c5c; }
.file-meta.generating { color: #80c8ff; }
.file-meta.muted { color: #666; }

.file-actions { display: flex; gap: 4px; }

/* Editor */
.file-editor-wrap { width: 100%; margin-top: 6px; }
.file-editor {
  width: 100%;
  box-sizing: border-box;
  background: #1a1a1a;
  border: 1px solid #444;
  border-radius: 4px;
  color: #d4d4d4;
  font-family: monospace;
  font-size: 12px;
  padding: 6px;
  resize: vertical;
}

/* Preview */
.file-preview-wrap { width: 100%; margin-top: 6px; }
.file-preview {
  background: #1a1a1a;
  border: 1px solid #333;
  border-radius: 4px;
  padding: 8px;
  font-size: 12px;
  white-space: pre-wrap;
  overflow-x: auto;
  max-height: 200px;
  overflow-y: auto;
}

/* Add-file row */
.add-file-row { background: transparent; justify-content: flex-start; }
.add-file-form {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  width: 100%;
  margin-top: 4px;
}
.add-file-input {
  background: #2a2a2a;
  border: 1px solid #555;
  border-radius: 4px;
  color: #d4d4d4;
  padding: 3px 8px;
  font-size: 13px;
  flex: 1;
  min-width: 140px;
}

/* Buttons */
.btn-primary, .btn-secondary {
  padding: 6px 14px;
  border-radius: 5px;
  border: none;
  cursor: pointer;
  font-size: 13px;
}
.btn-primary  { background: #3a7bd5; color: #fff; }
.btn-primary:hover  { background: #2e68c0; }
.btn-primary:disabled { background: #444; cursor: not-allowed; }
.btn-secondary { background: #3a3a3a; color: #d4d4d4; }
.btn-secondary:hover { background: #4a4a4a; }

.btn-small {
  padding: 2px 8px;
  border-radius: 3px;
  border: 1px solid #555;
  background: #2a2a2a;
  color: #bbb;
  cursor: pointer;
  font-size: 12px;
}
.btn-small:hover  { background: #3a3a3a; }
.btn-write        { border-color: #3a7bd5; color: #80c8ff; }
.btn-retry        { border-color: #c07030; color: #f0c060; }
.btn-skip         { border-color: #666; }
.btn-preview      { border-color: #666; }
.btn-add-file     { background: transparent; border: 1px dashed #555; color: #888; cursor: pointer; padding: 4px 10px; border-radius: 4px; font-size: 13px; }
.btn-add-file:hover { color: #d4d4d4; border-color: #888; }

/* Panel actions */
.panel-actions {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}

/* Post-creation status */
.post-status {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.post-item {
  padding: 4px 10px;
  border-radius: 4px;
  font-size: 13px;
  background: #252525;
}
.post-item.success { background: #1a2a1a; }
.post-item.fail    { background: #2a1a1a; }
.post-detail { display: block; font-size: 11px; color: #e05c5c; margin-top: 2px; }

/* Complete actions */
.complete-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.complete-actions a {
  display: inline-block;
  padding: 6px 14px;
  border-radius: 5px;
  background: #3a3a3a;
  color: #d4d4d4;
  text-decoration: none;
  font-size: 13px;
}
.complete-actions a:hover { background: #4a4a4a; }
</style>
