<template>
  <section class="e2e-builder" aria-live="polite">
    <header><div><small>E2E SAMPLE · LIVE</small><h3>正在创建 Runtime 待测样本</h3></div><span><i />{{ currentTitle }}</span></header>
    <div class="sample" v-if="reviewSample">
      <div class="sample-head"><strong>审阅样本</strong><em>由后端实际 E2E 会话返回</em></div>
      <dl><template v-for="row in sampleRows" :key="row[0]"><dt>{{ row[0] }}</dt><dd><code>{{ row[1] }}</code></dd></template></dl>
    </div>
    <div v-else class="sample-loading"><span /><div><strong>合成输入并进行语义审阅</strong><p>样本确定后会在这里显示，随后用于真实 Runtime 执行。</p></div></div>
    <div class="simple-track"><span :class="{on: activeIndex >= 0}">生成样本</span><i>→</i><span :class="{on: activeIndex >= 1}">审阅冻结</span><i>→</i><span :class="{on: activeIndex >= 2}">Runtime 执行</span></div>
    <p v-if="briefFailure" class="brief-failure">{{ briefFailure }}</p>
  </section>
</template>
<script setup>
import { computed } from 'vue'
const props = defineProps({ events: { type: Array, default: () => [] }, reviewSample: { type: Object, default: null } })
const e2e = computed(() => props.events.filter(event => event.stage === 'e2e' || event.stage === 'repair'))
const latest = computed(() => e2e.value.at(-1))
const currentTitle = computed(() => latest.value?.level === 'error' ? '需要处理' : '准备中')
const sampleRows = computed(() => Object.entries(props.reviewSample || {}).filter(([,v]) => v !== null && v !== '').slice(0, 10).map(([k,v]) => [k, typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v)]))
const activeIndex = computed(() => props.reviewSample ? 2 : e2e.value.length ? 0 : -1)
const briefFailure = computed(() => latest.value?.level === 'error' ? String(latest.value.message || '样本运行未通过，请在执行过程中查看简要原因').slice(0, 140) : '')
</script>
<style scoped>
.e2e-builder{padding:20px;border:1px solid #ddd6fe;border-radius:18px;background:linear-gradient(145deg,#fff,#faf5ff);box-shadow:0 14px 35px rgba(109,40,217,.08)}header{display:flex;justify-content:space-between;gap:12px}h3{margin:4px 0;font-size:17px}small{color:#7c3aed;font-weight:800;letter-spacing:.12em}header>span{font-size:11px;color:#6d28d9}header i{display:inline-block;width:7px;height:7px;margin-right:6px;border-radius:50%;background:#8b5cf6;animation:pulse 1.2s infinite}.sample,.sample-loading{margin:14px 0;padding:13px;border-radius:12px;background:#fff;border:1px solid #ede9fe}.sample-head{display:flex;justify-content:space-between;font-size:11px}.sample-head em{color:var(--text-muted);font-style:normal}.sample dl{display:grid;grid-template-columns:auto 1fr;gap:7px 12px;margin:12px 0 0;font-size:10px}.sample dt{color:#7c3aed;font-weight:700}.sample dd{margin:0;max-height:80px;overflow:auto;white-space:pre-wrap;word-break:break-word}.sample-loading{display:flex;gap:12px;align-items:center}.sample-loading span{width:26px;height:26px;border:3px solid #ddd6fe;border-top-color:#7c3aed;border-radius:50%;animation:spin .8s linear infinite}.sample-loading strong{font-size:12px}.sample-loading p{margin:3px 0;color:var(--text-muted);font-size:10px}.simple-track{display:flex;align-items:center;justify-content:center;gap:9px;color:#c4b5fd;font-size:11px}.simple-track span{padding:5px 9px;border-radius:999px;background:#f5f3ff}.simple-track span.on{color:#6d28d9;background:#ede9fe;font-weight:700}.brief-failure{margin:12px 0 0;padding:9px;border-radius:8px;background:#fef2f2;color:#991b1b;font-size:11px}@keyframes spin{to{transform:rotate(360deg)}}@keyframes pulse{50%{opacity:.3}}
</style>
