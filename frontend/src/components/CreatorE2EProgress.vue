<template>
  <section class="progress-card" aria-live="polite">
    <header><div><small>LIVE E2E</small><h3>E2E 运行过程</h3></div><span>{{ currentTitle }}</span></header>
    <div class="track"><template v-for="(label,index) in stages" :key="label"><b :class="{ active:index<=activeIndex }">{{ label }}</b><i v-if="index<stages.length-1">↓</i></template></div>
    <div v-if="inputs.length" class="block"><strong>待测输入</strong><article v-for="input in inputs" :key="input.filename"><code>{{ input.filename }}</code><pre>{{ input.preview || '无预览' }}</pre></article></div>
    <div v-if="script" class="block"><strong>执行脚本</strong><code>{{ script }}</code></div>
    <div v-if="logs" class="block"><strong>stdout / stderr</strong><pre>{{ logs }}</pre></div>
    <div v-if="failure" class="block failure"><strong>失败原因</strong><p>{{ failure }}</p></div>
    <div v-if="repair" class="block repair"><strong>Repair 建议</strong><p>{{ repair }}</p></div>
  </section>
</template>
<script setup>
import { computed } from 'vue'
const props = defineProps({ events: { type: Array, default: () => [] } })
const stages=['准备测试输入','生成测试文件','执行 Skill','采集 stdout/stderr','Failure 分析','Repair']
const e2e = computed(() => props.events.filter(event => ['e2e','repair'].includes(event.stage)))
const latest = computed(() => e2e.value.at(-1))
const payloads = computed(() => e2e.value.map(event => event.payload || {}))
const findLast = keys => { for(const payload of [...payloads.value].reverse()) for(const key of keys) if(payload[key]) return payload[key]; return '' }
const inputs = computed(() => { const value=findLast(['generated_inputs','test_inputs']); return Array.isArray(value)?value:[] })
const script = computed(() => findLast(['script_path','execution_script','command']))
const logs = computed(() => [findLast(['stdout_summary','stdout']),findLast(['stderr_summary','stderr'])].filter(Boolean).join('\n'))
const failure = computed(() => findLast(['failure_summary','reason','error','message']) || (latest.value?.level==='error'?latest.value.message:''))
const repair = computed(() => findLast(['repair_result','repair_suggestion','next_target','diff_excerpt']))
const currentTitle = computed(() => latest.value?.title || '等待运行')
const activeIndex = computed(() => repair.value?5:failure.value?4:logs.value?3:script.value?2:inputs.value.length?1:e2e.value.length?0:-1)
</script>
<style scoped>
.progress-card{padding:16px;border:1px solid var(--border);border-radius:12px;background:var(--surface)}header{display:flex;justify-content:space-between;gap:10px}h3{margin:2px 0;font-size:15px}small{color:#7c3aed;font-weight:800;letter-spacing:.12em}header>span{font-size:11px;color:var(--text-muted)}.track{display:grid;gap:3px;margin:12px 0}.track b{font-size:11px;color:var(--text-muted)}.track b.active{color:#6d28d9}.track i{font-size:9px;color:#c4b5fd}.block{margin-top:10px;padding:9px;border-radius:8px;background:var(--surface2);font-size:11px}.block>strong{display:block;margin-bottom:6px}.block code{word-break:break-all}.block pre{max-height:100px;margin:5px 0 0;overflow:auto;white-space:pre-wrap}.block p{margin:0}.failure{background:#fef2f2;color:#991b1b}.repair{background:#fffbeb;color:#92400e}
</style>
