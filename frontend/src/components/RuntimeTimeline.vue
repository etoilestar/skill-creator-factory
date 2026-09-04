<template>
  <section class="runtime-status" aria-label="Creator 运行状态">
    <strong>Creator 运行</strong>
    <div class="stage-strip">
      <span v-for="stage in visibleStages" :key="stage.key" :class="stage.status">
        {{ stage.label }} <b>{{ statusIcon(stage.status) }}</b>
      </span>
    </div>
    <span class="current-status" :class="activeStage?.status">
      {{ activeStage?.detail || statusLabel(activeStage?.status) }}
    </span>
  </section>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({ stages: { type: Array, default: () => [] } })
const activeStage = computed(() => props.stages.find(stage => stage.status === 'failed') || props.stages.find(stage => stage.status === 'running') || [...props.stages].reverse().find(stage => stage.status === 'success') || props.stages[0])
const visibleStages = computed(() => props.stages.filter(stage => stage.status !== 'pending' || stage === activeStage.value).slice(-3))
function statusIcon(status) { return ({ pending: '○', running: '…', success: '✓', failed: '×' })[status] || '○' }
function statusLabel(status) { return ({ pending: '等待开始', running: '执行中', success: '已完成', failed: '运行失败' })[status] || '等待开始' }
</script>

<style scoped>
.runtime-status { display: flex; align-items: center; gap: 14px; min-height: 52px; max-height: 60px; padding: 8px 24px; border-bottom: 1px solid var(--border); background: var(--surface); overflow: hidden; flex-shrink: 0; }
.runtime-status > strong { white-space: nowrap; font-size: 13px; }.stage-strip { display: flex; min-width: 0; gap: 8px; overflow: hidden; }.stage-strip span { padding-right: 8px; border-right: 1px solid var(--border); color: var(--text-muted); font-size: 12px; white-space: nowrap; }.stage-strip .success b { color: #16a34a; }.stage-strip .running b { color: #2563eb; }.stage-strip .failed b { color: #dc2626; }.current-status { margin-left: auto; max-width: 34%; overflow: hidden; padding: 4px 9px; border-radius: 999px; background: #e2e8f0; color: #475569; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }.current-status.running { background: #dbeafe; color: #1d4ed8; }.current-status.success { background: #dcfce7; color: #166534; }.current-status.failed { background: #fee2e2; color: #991b1b; }
@media (max-width: 720px) { .runtime-status { padding-inline: 12px; gap: 8px; }.stage-strip span:nth-child(-n+2) { display: none; }.current-status { max-width: 45%; } }
</style>
