<template>
  <div class="tool-registry page-scroll">
    <header class="hero">
      <div>
        <p class="eyebrow">Creator Tool Registry</p>
        <h1>在线工具制作 / 注册</h1>
        <p class="muted">统一 Tool Authoring 链路：描述需求、可选粘贴代码，确认 adapter 和 snippet 后再注册。</p>
      </div>
      <button class="btn-ghost" @click="loadTools">刷新工具列表</button>
    </header>

    <div class="tool-workspace">
      <aside class="step-sidebar" aria-label="Tool authoring steps">
        <button
          v-for="step in steps"
          :key="step.key"
          type="button"
          class="step-nav"
          :class="{ active: activeStep === step.key }"
          @click="activeStep = step.key"
        >
          <span class="step-index">{{ step.index }}</span>
          <span class="step-meta">
            <strong>{{ step.title }}</strong>
            <small>{{ step.description }}</small>
          </span>
          <span v-if="stepStatus(step.key)" class="status-dot" :class="stepStatus(step.key)"></span>
        </button>
      </aside>

      <main class="step-main">
        <section v-show="activeStep === 'input'" class="workspace-card">
          <div class="card-heading">
            <p class="eyebrow">Step 1</p>
            <h2>需求 / 可选代码</h2>
            <p class="muted small">先描述工具目标；如果已有 Python 实现，直接粘贴代码，planner 会优先理解代码里的真实 IO。</p>
          </div>
          <div class="form-row">
            <label>工具名称<input v-model="form.tool_name" placeholder="markdown_to_pdf" /></label>
            <label>工具用途描述<SmartCodeEditor v-model="form.description" language="markdown" min-height="140px" placeholder="我想注册一个工具，用来把 markdown 转成 PDF..." /></label>
          </div>
          <label>可选：粘贴已有 Python 代码<SmartCodeEditor v-model="optionalCodeBlock" language="python" min-height="260px" placeholder="def save_markdown(payload): ..." /></label>

          <CollapsiblePanel v-model:open="advancedOpen" title="高级选项" description="工具类型、角色、输入输出描述、权限和风险声明" :badge="advancedBadge">
            <div class="form-row relaxed">
              <label>工具类型<select v-model="form.tool_type"><option v-for="type in toolTypes" :key="type" :value="type">{{ type }}</option></select></label>
              <label>允许角色<input v-model="allowedRolesText" placeholder="pdf_builder,document_generator" /></label>
            </div>
            <div class="form-row relaxed">
              <label>输入描述<SmartCodeEditor v-model="form.input_description" language="text" min-height="100px" /></label>
              <label>输出描述<SmartCodeEditor v-model="form.output_description" language="text" min-height="100px" /></label>
            </div>
            <div class="checks">
              <label><input v-model="form.needs_secret" type="checkbox" /> 需要密钥</label>
              <label><input v-model="form.needs_external_network" type="checkbox" /> 需要外部网络</label>
              <label><input v-model="form.generates_file" type="checkbox" /> 生成文件</label>
              <label><input v-model="form.high_risk" type="checkbox" /> 高风险</label>
            </div>
          </CollapsiblePanel>

          <div class="step-actions">
            <button class="btn-ghost" :disabled="busy" @click="draftManifest">旧版规则草稿</button>
            <button class="btn-primary" :disabled="busy" @click="authorDraft">智能生成工具草稿</button>
          </div>
        </section>

        <section v-show="activeStep === 'planner'" class="workspace-card editor-card">
          <div class="card-heading">
            <p class="eyebrow">Step 2</p>
            <h2>Planner / Manifest</h2>
            <p class="muted small">检查 planner 推断的 manifest、输入输出 schema 和安全声明；如有追问，先回到需求补充信息。</p>
          </div>
          <div v-if="clarificationQuestions.length" class="validation bad">
            <strong>需要补充信息</strong>
            <ul><li v-for="question in clarificationQuestions" :key="question">{{ question }}</li></ul>
          </div>
          <SmartCodeEditor v-model="manifestText" language="json" min-height="520px" placeholder="Planner 生成的 manifest JSON" />
          <div class="step-actions">
            <button class="btn-ghost" @click="activeStep = 'input'">返回需求</button>
            <button class="btn-primary" :disabled="!parsedManifest" @click="activeStep = 'adapter'">继续到 Adapter</button>
          </div>
        </section>

        <section v-show="activeStep === 'adapter'" class="workspace-card editor-card">
          <div class="card-heading">
            <p class="eyebrow">Step 3</p>
            <h2>Adapter 实现</h2>
            <p class="muted small">确认或编辑最终 Python adapter。Finalize 会重新跑 dynamic trial，通过后才生成 snippet。</p>
          </div>
          <SmartCodeEditor v-model="adapterCode" language="python" min-height="560px" placeholder="Python adapter code" />
          <div class="step-actions">
            <button class="btn-ghost" :disabled="busy || !parsedManifest" @click="generateCode">旧版生成实现</button>
            <button class="btn-primary" :disabled="busy || !parsedManifest || !adapterCode" @click="finalizeAuthoring">确认代码 → 生成 snippet</button>
          </div>
        </section>

        <section v-show="activeStep === 'validation'" class="workspace-card">
          <div class="card-heading">
            <p class="eyebrow">Step 4</p>
            <h2>验证结果</h2>
            <p class="muted small">使用 sample input 运行动态验证；function card 预览可折叠查看。</p>
          </div>
          <label>Sample input<SmartCodeEditor v-model="sampleInputText" language="json" min-height="200px" /></label>
          <div class="actions">
            <button class="btn-primary" :disabled="busy || !parsedManifest" @click="validateTool">运行验证</button>
            <button class="btn-ghost" :disabled="!snippetReady" @click="activeStep = 'snippet'">查看 Snippet</button>
          </div>
          <div v-if="lastValidation" class="validation" :class="lastValidation.success ? 'ok' : 'bad'">
            <strong>{{ lastValidation.success ? '验证通过' : '验证失败' }}</strong>
            <ul><li v-for="err in lastValidation.errors" :key="err">{{ err }}</li></ul>
            <p v-for="warn in lastValidation.warnings" :key="warn" class="warn">{{ warn }}</p>
          </div>
          <div v-if="snippetReady" class="compact-preview">Snippet 已生成，请到「5 Snippet」确认后注册。</div>
          <CollapsiblePanel v-model:open="previewOpen" title="Creator function card preview" description="模型注入时看到的函数卡片预览">
            <pre class="tool-card">{{ cardPreview }}</pre>
          </CollapsiblePanel>
          <div class="step-actions">
            <button class="btn-ghost" @click="activeStep = 'adapter'">返回 Adapter</button>
            <button class="btn-primary" :disabled="!snippetReady" @click="activeStep = 'snippet'">继续确认 Snippet</button>
          </div>
        </section>

        <section v-show="activeStep === 'snippet'" class="workspace-card editor-card">
          <div class="card-heading">
            <p class="eyebrow">Step 5</p>
            <h2>Snippet 确认</h2>
            <p class="muted small">这里是唯一的注册用 snippet 编辑入口；注册前会从当前内容覆盖 manifest.snippets。</p>
          </div>
          <SmartCodeEditor v-model="snippetText" language="json" min-height="520px" placeholder="确认代码后生成 snippet" />
          <div class="step-actions">
            <button class="btn-ghost" @click="activeStep = 'validation'">返回验证</button>
            <button class="btn-primary" :disabled="busy || !lastValidation?.success" @click="registerTool">使用当前 snippet 注册工具</button>
          </div>
        </section>

        <section v-show="activeStep === 'registeredTools'" class="workspace-card">
          <div class="card-heading">
            <p class="eyebrow">Step 6</p>
            <h2>已启用 / 已注册工具</h2>
            <p class="muted small">注册后工具会出现在这里；也可以进入底部 Snippet Manager 管理额外用法片段。</p>
          </div>
          <div class="tools-table">
            <div class="row head"><span>名称</span><span>策略</span><span>状态</span><span>函数卡</span></div>
            <div v-for="tool in tools" :key="tool.name" class="row">
              <span><strong>{{ tool.name }}</strong><small>{{ tool.display_name }}</small></span>
              <span>{{ tool.usage_policy }}</span>
              <span :class="tool.creator_available ? 'green' : 'muted'">{{ tool.creator_available ? 'enabled' : 'disabled' }}</span>
              <span>{{ (tool.functions || []).map(fn => fn.function_name).join(', ') || tool.helper_imports?.join(', ') }}</span>
            </div>
          </div>
        </section>
      </main>
    </div>

    <CollapsiblePanel v-model:open="snippetManagerOpen" title="Snippet Manager" description="管理已注册工具的额外 Tool Usage Snippets" :badge="snippets.length">
      <div class="grid two">
        <div class="step-card">
          <p class="muted small">编辑模型会看到的 import、最小调用、返回规则和反例；可预览 Creator 注入格式并做静态 smoke test。</p>
          <div class="form-row relaxed">
            <label>选择工具<select v-model="selectedToolName" @change="loadSnippets"><option value="">选择工具</option><option v-for="tool in tools" :key="tool.name" :value="tool.name">{{ tool.name }}</option></select></label>
            <label>Snippet 类型<select v-model="snippetForm.kind"><option v-for="kind in snippetKinds" :key="kind" :value="kind">{{ kind }}</option></select></label>
          </div>
          <div class="form-row relaxed">
            <label>ID<input v-model="snippetForm.id" placeholder="create_pdf.minimal_text_pdf" /></label>
            <label>标题<input v-model="snippetForm.title" placeholder="Create a simple PDF" /></label>
          </div>
          <label>适用 roles（逗号分隔）<input v-model="snippetRolesText" placeholder="pdf_builder,document_generator" /></label>
          <label>适用 capabilities（逗号分隔）<input v-model="snippetCapabilitiesText" placeholder="pdf_generation" /></label>
          <label>failure layers（逗号分隔）<input v-model="snippetFailuresText" placeholder="final_platform_output_value_invalid,artifact_missing" /></label>
          <label>描述<SmartCodeEditor v-model="snippetForm.description" language="markdown" min-height="90px" /></label>
          <label>正确调用代码<SmartCodeEditor v-model="snippetForm.code" language="python" min-height="220px" /></label>
          <div class="form-row relaxed">
            <label>期望输入 shape(JSON)<SmartCodeEditor v-model="snippetInputShapeText" language="json" min-height="160px" /></label>
            <label>期望输出 shape(JSON)<SmartCodeEditor v-model="snippetOutputShapeText" language="json" min-height="160px" /></label>
          </div>
          <label>return rule<SmartCodeEditor v-model="snippetForm.return_rule" language="text" min-height="90px" /></label>
          <label>anti patterns（每行一条）<SmartCodeEditor v-model="snippetAntiPatternsText" language="text" min-height="120px" /></label>
          <div class="form-row relaxed">
            <label>usage policy<select v-model="snippetForm.usage_policy"><option>helper_preferred</option><option>helper_required</option><option>self_implementation_allowed</option></select></label>
            <label>priority<input v-model.number="snippetForm.priority" type="number" /></label>
          </div>
          <div class="actions">
            <button class="btn-primary" :disabled="busy || !selectedToolName" @click="saveSnippet">新增 / 保存 snippet</button>
            <button class="btn-ghost" :disabled="busy || !selectedToolName || !snippetForm.id" @click="runSnippetSmokeTest">运行 smoke test</button>
          </div>
          <p v-if="snippetTestResult" class="small" :class="snippetTestResult.success ? 'green' : 'error'">Smoke test: {{ snippetTestResult.success ? 'passed' : 'failed' }} {{ snippetTestResult.message || '' }}</p>
        </div>

        <div class="step-card">
          <div class="snippets-list">
            <button v-for="snippet in snippets" :key="snippet.id" class="snippet-item" @click="editSnippet(snippet)">
              <strong>{{ snippet.id }}</strong><small>{{ snippet.kind }} · priority {{ snippet.priority }}</small>
            </button>
          </div>
          <pre class="tool-card">{{ snippetPreview }}</pre>
        </div>
      </div>
    </CollapsiblePanel>

    <p v-if="error" class="error">{{ error }}</p>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import CollapsiblePanel from '../components/CollapsiblePanel.vue'
import SmartCodeEditor from '../components/SmartCodeEditor.vue'
import { authorCreatorTool, createCreatorToolSnippet, draftCreatorTool, generateCreatorToolCode, listCreatorToolSnippets, listCreatorTools, registerCreatorTool, testCreatorToolSnippet, updateCreatorToolSnippet, validateCreatorTool } from '../composables/useCreator.js'

const toolTypes = ['python_helper', 'http_api', 'local_command', 'database_query', 'file_converter', 'document_generator', 'image_generator', 'custom_adapter']
const snippetKinds = ['minimal_usage', 'multi_input_usage', 'file_output_usage', 'batch_usage', 'error_repair_usage', 'anti_pattern', 'trial_run_usage']
const steps = [
  { key: 'input', index: 1, title: '需求 / 代码', description: '描述功能或粘贴代码' },
  { key: 'planner', index: 2, title: 'Manifest', description: '确认工具定义' },
  { key: 'adapter', index: 3, title: 'Adapter', description: '确认实现代码' },
  { key: 'validation', index: 4, title: '验证', description: '动态试运行' },
  { key: 'snippet', index: 5, title: 'Snippet', description: '确认用法片段' },
  { key: 'registeredTools', index: 6, title: '注册工具', description: '查看工具列表' },
]
const busy = ref(false)
const error = ref('')
const tools = ref([])
const allowedRolesText = ref('')
const manifestText = ref('')
const adapterCode = ref('')
const optionalCodeBlock = ref('')
const snippetText = ref('{}')
const clarificationQuestions = ref([])
const authorStage = ref('draft')
const activeStep = ref('input')
const advancedOpen = ref(false)
const previewOpen = ref(false)
const snippetManagerOpen = ref(false)
const sampleInputText = ref(`{
  "payload": {}
}`)
const lastValidation = ref(null)
const selectedToolName = ref('')
const snippets = ref([])
const snippetRolesText = ref('')
const snippetCapabilitiesText = ref('')
const snippetFailuresText = ref('')
const snippetInputShapeText = ref('{}')
const snippetOutputShapeText = ref('{}')
const snippetAntiPatternsText = ref('')
const snippetTestResult = ref(null)
const snippetForm = reactive({ id: '', title: '', kind: 'minimal_usage', description: '', code: '', return_rule: '', usage_policy: 'helper_preferred', priority: 80 })
const expandedPanels = reactive({ input: true, planner: false, adapter: false, validation: false, snippet: false, registeredTools: false, snippetManager: false })

const form = reactive({ tool_name: '', description: '', tool_type: 'python_helper', input_description: '', output_description: '', needs_secret: false, needs_external_network: false, generates_file: false, high_risk: false })
const parsedManifest = computed(() => { try { return manifestText.value ? JSON.parse(manifestText.value) : null } catch { return null } })
const parsedSample = computed(() => { try { return sampleInputText.value ? JSON.parse(sampleInputText.value) : {} } catch { return {} } })
const cardPreview = computed(() => (lastValidation.value?.tool_card_preview || []).join('\n\n---\n\n') || '验证后展示 Creator prompt 注入的 function card。')
const snippetPreview = computed(() => snippets.value.map(snippet => snippet.formatted || '').join('\n\n---\n\n') || '选择工具后展示 Creator 会看到的 Tool Snippet。')
const snippetReady = computed(() => snippetText.value && snippetText.value.trim() !== '{}')
const advancedBadge = computed(() => [form.needs_secret && 'secret', form.needs_external_network && 'network', form.generates_file && 'file', form.high_risk && 'risk'].filter(Boolean).join(' · '))

function stepStatus(key) {
  if (key === 'planner' && parsedManifest.value) return 'ok'
  if (key === 'adapter' && adapterCode.value) return 'ok'
  if (key === 'validation' && lastValidation.value) return lastValidation.value.success ? 'ok' : 'bad'
  if (key === 'snippet' && snippetReady.value) return 'ok'
  return ''
}
async function run(task) { busy.value = true; error.value = ''; try { await task() } catch (e) { error.value = e.message || String(e) } finally { busy.value = false } }
async function loadTools() { const data = await listCreatorTools(); tools.value = data.tools || [] }
function payload() { return { ...form, allowed_roles: allowedRolesText.value.split(',').map(s => s.trim()).filter(Boolean) } }
function authorPayload(stage) { return { ...payload(), stage, code_block: optionalCodeBlock.value || (stage === 'draft' ? adapterCode.value : undefined), adapter_code: adapterCode.value, sample_input: parsedSample.value, manifest: parsedManifest.value, validation: lastValidation.value } }
function draftManifest() { return run(async () => { const data = await draftCreatorTool(payload()); manifestText.value = JSON.stringify(data.manifest, null, 2); lastValidation.value = null; clarificationQuestions.value = []; activeStep.value = 'planner' }) }
function authorDraft() { return run(async () => { const data = await authorCreatorTool(authorPayload('draft')); authorStage.value = 'draft'; clarificationQuestions.value = data.questions || []; manifestText.value = JSON.stringify(data.manifest || {}, null, 2); sampleInputText.value = JSON.stringify(data.sample_input || {}, null, 2); adapterCode.value = data.adapter_code || adapterCode.value; lastValidation.value = data.validation || null; snippetText.value = data.snippet ? JSON.stringify(data.snippet, null, 2) : snippetText.value; activeStep.value = data.needs_clarification ? 'planner' : 'adapter' }) }
function finalizeAuthoring() { return run(async () => { const data = await authorCreatorTool(authorPayload('finalize')); authorStage.value = 'finalize'; lastValidation.value = data.validation || lastValidation.value; snippetText.value = data.snippet ? JSON.stringify(data.snippet, null, 2) : snippetText.value; if (data.snippet && parsedManifest.value) { const manifest = { ...parsedManifest.value, snippets: [data.snippet] }; manifestText.value = JSON.stringify(manifest, null, 2) } activeStep.value = data.snippet ? 'snippet' : 'validation' }) }
function generateCode() { return run(async () => { const data = await generateCreatorToolCode({ manifest: parsedManifest.value }); adapterCode.value = data.adapter_code; activeStep.value = 'adapter' }) }
function validateTool() { return run(async () => { lastValidation.value = await validateCreatorTool({ manifest: parsedManifest.value, adapter_code: adapterCode.value, sample_input: parsedSample.value, dynamic: true }); activeStep.value = 'validation' }) }
function buildFinalManifestForRegister() { const manifest = { ...(parsedManifest.value || {}) }; const snippet = parseJsonText(snippetText.value); if (snippet && Object.keys(snippet).length) manifest.snippets = [snippet]; return manifest }
function registerTool() { return run(async () => { await registerCreatorTool({ manifest: buildFinalManifestForRegister(), adapter_code: adapterCode.value, sample_input: parsedSample.value, dynamic: true, enable: true }); await loadTools(); activeStep.value = 'registeredTools' }) }
function parseJsonText(text) { try { return text ? JSON.parse(text) : {} } catch { return {} } }
function splitLines(text) { return text.split('\n').map(s => s.trim()).filter(Boolean) }
function splitCsv(text) { return text.split(',').map(s => s.trim()).filter(Boolean) }
function buildSnippetPayload() { return { ...snippetForm, applies_to: { roles: splitCsv(snippetRolesText.value), capabilities: splitCsv(snippetCapabilitiesText.value), failure_layers: splitCsv(snippetFailuresText.value) }, expected_input_shape: parseJsonText(snippetInputShapeText.value), expected_output_shape: parseJsonText(snippetOutputShapeText.value), anti_patterns: splitLines(snippetAntiPatternsText.value), requires: splitCsv(snippetCapabilitiesText.value) } }
function loadSnippets() { return run(async () => { if (!selectedToolName.value) { snippets.value = []; return } const data = await listCreatorToolSnippets(selectedToolName.value); snippets.value = data.snippets || []; snippetTestResult.value = null; snippetManagerOpen.value = true }) }
function editSnippet(snippet) { snippetForm.id = snippet.id; snippetForm.title = snippet.title; snippetForm.kind = snippet.kind || 'minimal_usage'; snippetForm.description = snippet.description || ''; snippetForm.code = snippet.code || ''; snippetForm.return_rule = snippet.return_rule || ''; snippetForm.usage_policy = snippet.usage_policy || 'helper_preferred'; snippetForm.priority = snippet.priority || 0; snippetRolesText.value = (snippet.applies_to?.roles || []).join(','); snippetCapabilitiesText.value = (snippet.applies_to?.capabilities || snippet.requires || []).join(','); snippetFailuresText.value = (snippet.applies_to?.failure_layers || []).join(','); snippetInputShapeText.value = JSON.stringify(snippet.expected_input_shape || {}, null, 2); snippetOutputShapeText.value = JSON.stringify(snippet.expected_output_shape || {}, null, 2); snippetAntiPatternsText.value = (snippet.anti_patterns || []).join('\n'); snippetTestResult.value = null; snippetManagerOpen.value = true }
function saveSnippet() { return run(async () => { const payload = buildSnippetPayload(); const exists = snippets.value.some(item => item.id === payload.id); if (exists) await updateCreatorToolSnippet(selectedToolName.value, payload.id, payload); else await createCreatorToolSnippet(selectedToolName.value, payload); await loadSnippets() }) }
function runSnippetSmokeTest() { return run(async () => { if (!snippets.value.some(item => item.id === snippetForm.id)) await saveSnippet(); snippetTestResult.value = await testCreatorToolSnippet(selectedToolName.value, snippetForm.id) }) }

onMounted(loadTools)
</script>

<style scoped>
.page-scroll { overflow: auto; padding: 24px; display: flex; flex-direction: column; gap: 20px; }
.tool-registry { max-width: 1440px; margin: 0 auto; }
.hero, .card, .workspace-card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; }
.hero { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; padding: 22px 24px; }
.eyebrow { color: var(--accent); text-transform: uppercase; letter-spacing: .08em; font-size: 12px; margin: 0 0 4px; }
h1 { font-size: 26px; margin: 2px 0 6px; } h2 { font-size: 20px; margin: 0 0 4px; }
.tool-workspace { display: grid; grid-template-columns: 270px minmax(0, 1fr); gap: 20px; align-items: start; }
.step-sidebar { position: sticky; top: 16px; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.step-nav { display: grid; grid-template-columns: 34px minmax(0, 1fr) auto; gap: 10px; align-items: center; text-align: left; border: 1px solid var(--border); background: var(--surface); color: var(--text); border-radius: 14px; padding: 12px; cursor: pointer; }
.step-nav.active { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, var(--surface)); }
.step-index { width: 30px; height: 30px; border-radius: 50%; background: var(--surface2); display: grid; place-items: center; font-weight: 800; }
.step-meta { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.step-meta strong { font-size: 14px; } .step-meta small { color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.status-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--text-muted); }
.status-dot.ok { background: var(--success); } .status-dot.bad { background: var(--danger); }
.step-main { min-width: 0; }
.workspace-card { min-height: calc(100vh - 180px); padding: 24px; display: flex; flex-direction: column; gap: 18px; }
.card-heading { display: flex; flex-direction: column; gap: 2px; }
.editor-card .smart-editor { flex: 1; }
.editor-card :deep(.editor-shell) { min-height: 520px; }
.grid { display: grid; gap: 20px; } .grid.two { grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); }
.step-card { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.form-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; align-items: start; }
.form-row.relaxed { gap: 18px; }
label { display: flex; flex-direction: column; gap: 8px; color: var(--text-muted); min-width: 0; }
.checks { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }
.checks label { flex-direction: row; align-items: center; } .checks input { width: auto; }
.actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.step-actions { position: sticky; bottom: 0; margin: auto -24px -24px; padding: 14px 24px; background: color-mix(in srgb, var(--surface) 92%, transparent); border-top: 1px solid var(--border); display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 10px; z-index: 1; }
.small { font-size: 12px; }
.validation, .compact-preview { padding: 12px; border-radius: var(--radius); border: 1px solid var(--border); }
.validation.ok, .compact-preview { border-color: var(--success); } .validation.bad { border-color: var(--danger); }
.warn { color: #f6c177; }
.tool-card { white-space: pre-wrap; overflow: auto; max-height: 420px; background: var(--surface2); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px; }
.tools-table, .snippets-list { display: grid; gap: 8px; }
.snippet-item { text-align: left; background: var(--surface2); border: 1px solid var(--border); border-radius: var(--radius); padding: 10px; color: var(--text); cursor: pointer; }
.row { display: grid; grid-template-columns: minmax(180px, 1.2fr) minmax(120px, .8fr) minmax(100px, .6fr) minmax(220px, 1.4fr); gap: 16px; align-items: start; padding: 12px; border-bottom: 1px solid var(--border); }
.row.head { color: var(--text-muted); font-size: 12px; text-transform: uppercase; } small { display: block; color: var(--text-muted); } .green { color: var(--success); }
@media (max-width: 960px) { .tool-workspace { grid-template-columns: 1fr; } .step-sidebar { position: static; flex-direction: row; overflow-x: auto; padding-bottom: 4px; } .step-nav { min-width: 220px; } .workspace-card { min-height: auto; } }
@media (max-width: 900px) { .row, .row.head { grid-template-columns: 1fr; } .row.head { display: none; } .hero { flex-direction: column; } }
@media (max-width: 560px) { .page-scroll { padding: 14px; } .workspace-card { padding: 16px; } .step-actions { margin: auto -16px -16px; padding: 12px 16px; } .grid.two { grid-template-columns: 1fr; } }
</style>
