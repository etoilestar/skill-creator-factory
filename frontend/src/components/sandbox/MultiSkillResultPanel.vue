<template>
  <section class="result-panel">
    <div v-if="!result" class="muted empty-state">执行结果将在这里汇总。</div>
    <template v-else>
      <h4>{{ modeLabel }}</h4>
      <div v-if="result.reason" class="notice">{{ result.reason }}</div>
      <div v-if="result.missing?.length" class="notice">需要补充：{{ result.missing.join('、') }}</div>
      <div v-if="result.failed_skill_step_id" class="failure">失败步骤：{{ result.failed_skill_step_id }}</div>
      <div v-if="result.paused_at_step_id" class="notice">暂停步骤：{{ result.paused_at_step_id }}</div>
      <h5>Skill 结果</h5>
      <div v-for="(item, id) in result.skill_results || {}" :key="id" class="summary"><code>{{ id }}</code><span>{{ item.skill_name || item.status || item.mode || '已返回' }}</span></div>
      <h5 v-if="result.artifacts?.length || result.output_files?.length">产物</h5>
      <div v-for="file in [...(result.artifacts || []), ...(result.output_files || [])]" :key="file.url || file.path" class="summary">{{ file.name || file.filename || file.path }}</div>
      <details v-if="debug"><summary>调试事件（已过滤）</summary><pre>{{ safeDebug }}</pre></details>
    </template>
  </section>
</template>
<script setup>
import { computed } from 'vue'
const props = defineProps({ result: { type: Object, default: null }, trace: { type: Array, default: () => [] }, debug: Boolean })
const modeLabel = computed(() => props.result?.mode === 'ask_user' ? '⚠️ 等待用户输入' : props.result?.success === false ? '❌ 多技能执行失败' : '✅ 多技能执行结果')
const safeDebug = computed(() => JSON.stringify({ mode: props.result?.mode, success: props.result?.success, failed_skill_step_id: props.result?.failed_skill_step_id, paused_at_step_id: props.result?.paused_at_step_id, multi_skill_trace: props.trace }, null, 2))
</script>
<style scoped>
.result-panel{padding:14px;overflow:auto}.empty-state{text-align:center;padding:20px 0}.notice,.failure{padding:8px;margin:8px 0;border-radius:6px;background:#fffbeb;color:#92400e}.failure{background:#fef2f2;color:#991b1b}.summary{display:flex;gap:8px;padding:7px 0;border-bottom:1px solid var(--border);font-size:13px}h4,h5{margin:4px 0 8px}h5{margin-top:16px}pre{white-space:pre-wrap;font-size:11px}
</style>
