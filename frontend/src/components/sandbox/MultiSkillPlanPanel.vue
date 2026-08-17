<template>
  <section class="multi-plan">
    <header><strong>多技能调度方案</strong><span v-if="plan?.selection_mode">{{ plan.selection_mode }}</span></header>
    <ol>
      <li v-for="step in normalizedSteps" :key="step.step_id">
        <div class="skill"><code>{{ step.step_id }}</code><strong>{{ step.skill_name }}</strong></div>
        <p>{{ step.task || '完成用户任务' }}</p>
        <div v-if="step.depends_on?.length" class="flow">依赖：{{ step.depends_on.join('、') }}</div>
        <div v-if="sourceLabel(step)" class="flow">输入：{{ sourceLabel(step) }}</div>
      </li>
    </ol>
    <div v-if="confirmable" class="actions">
      <button class="btn-primary" :disabled="confirming" @click="$emit('confirm')">{{ confirming ? '确认中…' : '确认执行' }}</button>
      <button class="btn-ghost" :disabled="confirming" @click="$emit('cancel')">取消</button>
    </div>
  </section>
</template>
<script setup>
import { computed } from 'vue'
const props = defineProps({ plan: { type: Object, default: null }, confirming: Boolean, confirmable: Boolean })
defineEmits(['confirm', 'cancel'])
const normalizedSteps = computed(() => props.plan?.steps || props.plan?.preview || [])
function sourceLabel(step) {
  const sources = step.input_sources || step.input_bindings
  if (Array.isArray(sources)) return sources.join('、')
  if (sources && typeof sources === 'object') return Object.keys(sources).join('、')
  return ''
}
</script>
<style scoped>
.multi-plan{padding:14px;overflow:auto}.multi-plan header{display:flex;justify-content:space-between;gap:8px}.multi-plan header span,.flow{font-size:12px;color:var(--text-muted)}ol{padding-left:24px}li{padding:10px 0;border-bottom:1px solid var(--border)}.skill{display:flex;gap:8px;align-items:center}.skill code{font-size:11px}p{margin:6px 0;line-height:1.5}.actions{display:flex;gap:8px;margin-top:14px}
</style>
