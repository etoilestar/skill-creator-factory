<template>
  <div class="smart-editor" :class="[`lang-${language}`, density, { readonly, fill }]" :style="editorStyle">
    <div v-if="toolbar" class="editor-toolbar">
      <span class="language-label">{{ languageLabel }}</span>
      <span v-if="lintMessage" class="lint" :class="lintOk ? 'ok' : 'bad'">{{ lintMessage }}</span>
      <button v-if="lineWrapping" type="button" class="mini" @click="wrap = !wrap">{{ wrap ? '关闭换行' : '自动换行' }}</button>
    </div>
    <div class="editor-shell">
      <pre class="line-numbers" aria-hidden="true">{{ lineNumbers }}</pre>
      <pre
        ref="editable"
        class="editor-surface"
        :class="{ wrap }"
        :contenteditable="!readonly"
        :data-placeholder="placeholder"
        spellcheck="false"
        role="textbox"
        aria-multiline="true"
        @input="onInput"
        @keydown="onKeydown"
        @focus="showCompletions = true"
        @blur="scheduleHideCompletions"
      ></pre>
    </div>
    <div v-if="showCompletions && completionItems.length" class="completion-bar">
      <button v-for="item in completionItems" :key="item" type="button" @mousedown.prevent="insertCompletion(item)">{{ item }}</button>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  language: { type: String, default: 'text' },
  readonly: { type: Boolean, default: false },
  placeholder: { type: String, default: '' },
  minHeight: { type: String, default: '220px' },
  maxHeight: { type: String, default: '520px' },
  lineWrapping: { type: Boolean, default: true },
  completions: { type: Array, default: () => [] },
  diagnostics: { type: Array, default: () => [] },
  density: { type: String, default: 'comfortable' },
  toolbar: { type: Boolean, default: true },
  fill: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])
const editable = ref(null)
const wrap = ref(props.lineWrapping)
const showCompletions = ref(false)
const internal = ref(props.modelValue || '')

const defaultCompletions = {
  python: ['run', 'payload', 'OUTPUT_DIR', 'SKILL_TRIAL_RUN', 'file_paths', 'file_outputs', 'output_path', 'Path', 'json', 'os.environ'],
  json: ['id', 'title', 'kind', 'description', 'code', 'expected_input_shape', 'expected_output_shape', 'return_rule', 'anti_patterns', 'usage_policy', 'priority', 'name', 'display_name', 'tool_type', 'allowed_roles', 'required_capabilities', 'input_schema', 'output_schema', 'functions', 'snippets', 'adapter_path'],
  markdown: ['# ', '## ', '- ', '```python', '```json'],
}
const completionItems = computed(() => [...new Set([...(props.completions || []), ...(defaultCompletions[props.language] || [])])].slice(0, 18))
const languageLabel = computed(() => props.language === 'json' ? 'JSON' : props.language === 'python' ? 'Python' : props.language)
const editorStyle = computed(() => ({ '--min-height': props.minHeight, '--max-height': props.maxHeight }))
const lineNumbers = computed(() => Array.from({ length: Math.max(1, internal.value.split('\n').length) }, (_, i) => i + 1).join('\n'))
const lintOk = computed(() => props.language !== 'json' || !internal.value.trim() || !jsonError.value)
const jsonError = computed(() => {
  if (props.language !== 'json' || !internal.value.trim()) return ''
  try { JSON.parse(internal.value); return '' } catch (e) { return e.message || 'JSON 格式错误' }
})
const lintMessage = computed(() => {
  if (props.diagnostics?.length) return props.diagnostics.join(' · ')
  if (props.language === 'json') return jsonError.value || 'JSON OK'
  return ''
})

function syncDom(value) {
  if (!editable.value || editable.value.textContent === value) return
  editable.value.textContent = value
}
function onInput() {
  internal.value = editable.value?.textContent || ''
  emit('update:modelValue', internal.value)
}
function onKeydown(event) {
  if (event.key === 'Tab' && !props.readonly) {
    event.preventDefault()
    document.execCommand('insertText', false, '  ')
    onInput()
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
    showCompletions.value = true
  }
}
function insertCompletion(item) {
  if (props.readonly) return
  editable.value?.focus()
  document.execCommand('insertText', false, item)
  onInput()
}
function scheduleHideCompletions() { setTimeout(() => { showCompletions.value = false }, 150) }

watch(() => props.modelValue, value => {
  const next = value || ''
  if (next !== internal.value) {
    internal.value = next
    nextTick(() => syncDom(next))
  }
})
onMounted(() => syncDom(internal.value))
</script>

<style scoped>
.smart-editor { border: 1px solid var(--border); border-radius: var(--radius); background: #111827; overflow: hidden; min-height: var(--min-height); }
.smart-editor.fill { height: 100%; min-height: 0; display: flex; flex-direction: column; }
.editor-toolbar { min-height: 32px; display: flex; align-items: center; gap: 8px; justify-content: space-between; padding: 6px 10px; background: rgba(255,255,255,.04); border-bottom: 1px solid var(--border); color: var(--text-muted); font-size: 12px; }
.language-label { font-weight: 700; letter-spacing: .04em; color: var(--accent); }
.lint { margin-left: auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lint.ok { color: var(--success); } .lint.bad { color: var(--danger); }
.mini { border: 1px solid var(--border); background: transparent; color: var(--text-muted); border-radius: 8px; padding: 3px 8px; cursor: pointer; }
.editor-shell { display: grid; grid-template-columns: auto 1fr; max-height: var(--max-height); min-height: var(--min-height); overflow: auto; flex: 1; }
.smart-editor.fill .editor-shell { height: 100%; max-height: none; min-height: 0; }
.line-numbers, .editor-surface { margin: 0; font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; tab-size: 2; }
.line-numbers { user-select: none; text-align: right; color: #64748b; background: rgba(255,255,255,.03); border-right: 1px solid var(--border); padding: 10px 8px; min-width: 42px; }
.editor-surface { outline: none; color: #dbeafe; padding: 10px 12px; white-space: pre; min-width: 0; }
.editor-surface.wrap { white-space: pre-wrap; overflow-wrap: anywhere; }
.editor-surface:empty::before { content: attr(data-placeholder); color: #64748b; }
.lang-json .editor-surface { color: #fde68a; }
.lang-python .editor-surface { color: #bfdbfe; }
.lang-markdown .editor-surface { color: #d1fae5; }
.completion-bar { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px; border-top: 1px solid var(--border); background: rgba(15,23,42,.98); }
.completion-bar button { border: 1px solid var(--border); border-radius: 999px; background: rgba(255,255,255,.05); color: var(--text); padding: 3px 8px; cursor: pointer; font-size: 12px; }

.smart-editor.compact .editor-toolbar { min-height: 26px; padding: 4px 8px; }
.smart-editor.compact .line-numbers, .smart-editor.compact .editor-surface { font-size: 12px; line-height: 1.45; padding-top: 8px; padding-bottom: 8px; }
.smart-editor.compact .completion-bar { padding: 6px; }
.readonly .editor-surface { color: var(--text-muted); }
</style>
