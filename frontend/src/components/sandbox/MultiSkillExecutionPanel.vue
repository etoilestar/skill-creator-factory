<template>
  <section class="timeline">
    <div v-if="!steps.length" class="muted empty-state">等待系统生成技能级计划…</div>
    <article v-for="step in steps" :key="step.stepId" class="step" :class="step.status">
      <div class="line"><span class="icon">{{ icons[step.status] }}</span><strong>{{ step.skillName }}</strong><span class="status">{{ labels[step.status] }}</span></div>
      <p v-if="step.task">{{ step.task }}</p><code v-if="step.childRunId">{{ step.childRunId }}</code>
      <details v-if="step.childRunId && childEvents[step.childRunId]?.length">
        <summary>查看内部执行（{{ childEvents[step.childRunId].length }}）</summary>
        <div v-for="(event, i) in childEvents[step.childRunId]" :key="i" class="child-event">
          {{ eventLabel(event) }}
        </div>
      </details>
    </article>
  </section>
</template>
<script setup>
defineProps({ steps: { type: Array, default: () => [] }, childEvents: { type: Object, default: () => ({}) } })
const icons = { pending: '○', running: '●', completed: '✓', failed: '✕', ask_user: '!' }
const labels = { pending: '等待', running: '运行中', completed: '已完成', failed: '失败', ask_user: '等待用户输入' }
function eventLabel(event) { return event?.label || event?.message || event?.step || event?.type || '运行事件' }
</script>
<style scoped>
.timeline{padding:14px;overflow:auto}.empty-state{padding:20px 0;text-align:center}.step{position:relative;padding:8px 4px 18px 26px}.step:not(:last-child)::after{content:'↓';position:absolute;left:7px;bottom:-3px;color:var(--text-muted)}.line{display:flex;align-items:center;gap:8px}.icon{position:absolute;left:3px}.running .icon{color:#2563eb}.completed .icon{color:#16a34a}.failed .icon{color:#dc2626}.ask_user .icon{color:#d97706}.status{margin-left:auto;font-size:12px;color:var(--text-muted)}p{font-size:13px;margin:5px 0}code{font-size:11px;color:var(--text-muted)}details{margin-top:8px;font-size:12px}summary{cursor:pointer}.child-event{padding:5px 8px;margin-top:4px;background:var(--surface2);border-radius:4px;word-break:break-word}
</style>
