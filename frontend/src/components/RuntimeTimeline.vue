<template>
  <section class="runtime-timeline" aria-label="创建进度">
    <header>
      <div>
        <p class="eyebrow">PROCESS OVERVIEW</p>
        <h3>创建进度</h3>
      </div>
      <span class="status-pill" :class="activeStatus">{{ statusLabel(activeStatus) }}</span>
    </header>
    <ol>
      <li v-for="(stage, index) in stages" :key="stage.key" :class="stage.status">
        <span class="rail"><span class="marker">{{ statusIcon(stage.status) }}</span></span>
        <div class="stage-copy">
          <div class="stage-heading">
            <strong>{{ stage.label }}</strong>
            <span>{{ statusLabel(stage.status) }}</span>
          </div>
          <p v-if="stage.detail">{{ stage.detail }}</p>
          <div v-if="stage.items?.length" class="stage-items">
            <span v-for="item in stage.items.slice(0, 4)" :key="item">{{ item }}</span>
          </div>
        </div>
      </li>
    </ol>
  </section>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({ stages: { type: Array, default: () => [] } })
const activeStatus = computed(() => {
  if (props.stages.some(stage => stage.status === 'failed')) return 'failed'
  if (props.stages.some(stage => stage.status === 'running')) return 'running'
  if (props.stages.length && props.stages.every(stage => stage.status === 'success')) return 'success'
  return 'pending'
})
function statusIcon(status) { return ({ pending: '○', running: '●', success: '✓', failed: '×' })[status] || '○' }
function statusLabel(status) { return ({ pending: '等待中', running: '执行中', success: '已完成', failed: '失败' })[status] || '等待中' }
</script>

<style scoped>
.runtime-timeline { margin: 0 0 18px; padding: 18px 20px; border: 1px solid var(--border); border-radius: 16px; background: linear-gradient(145deg, var(--surface), var(--surface2, #f8fafc)); box-shadow: 0 10px 28px rgb(15 23 42 / 5%); }
header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
h3, .eyebrow { margin: 0; }.eyebrow { margin-bottom: 3px; color: #64748b; font-size: 10px; font-weight: 800; letter-spacing: .14em; }
.status-pill { padding: 4px 10px; border-radius: 999px; background: #e2e8f0; color: #475569; font-size: 12px; font-weight: 700; }
.status-pill.running { background: #dbeafe; color: #1d4ed8; }.status-pill.success { background: #dcfce7; color: #166534; }.status-pill.failed { background: #fee2e2; color: #991b1b; }
ol { display: grid; grid-template-columns: repeat(5, 1fr); gap: 0; margin: 0; padding: 0; list-style: none; }
li { position: relative; display: flex; min-width: 0; gap: 9px; padding-right: 10px; }
.rail { position: relative; flex: 0 0 24px; }.rail::after { position: absolute; top: 12px; left: 24px; width: calc(100vw / 8 - 34px); height: 2px; background: var(--border); content: ''; }
li:last-child .rail::after { display: none; }.marker { position: relative; z-index: 1; display: grid; width: 24px; height: 24px; place-items: center; border: 2px solid #cbd5e1; border-radius: 50%; background: var(--surface); color: #94a3b8; font-size: 12px; font-weight: 900; }
li.running .marker { border-color: #3b82f6; color: #2563eb; box-shadow: 0 0 0 4px #dbeafe; }.success .marker { border-color: #22c55e; background: #22c55e; color: white; }.failed .marker { border-color: #ef4444; background: #ef4444; color: white; }
.stage-copy { min-width: 0; }.stage-heading { display: grid; gap: 2px; }.stage-heading strong { overflow: hidden; font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }.stage-heading span, p { color: var(--text-muted); font-size: 11px; }p { margin: 5px 0 0; line-height: 1.4; }.stage-items { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }.stage-items span { max-width: 100%; overflow: hidden; padding: 2px 5px; border-radius: 4px; background: rgb(148 163 184 / 12%); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 900px) { ol { grid-template-columns: 1fr; gap: 8px; }.rail::after { top: 24px; left: 11px; width: 2px; height: calc(100% + 8px); }li { min-height: 42px; }.stage-heading { display: flex; justify-content: space-between; } }
</style>
