<template>
  <div class="creator-responsibility-node">
    <Handle type="target" :position="Position.Top" />
    <div class="responsibility-node-header">
      <strong :title="data.targetFile">{{ data.fileName }}</strong>
      <span v-if="data.role" class="role-badge">{{ data.role }}</span>
    </div>
    <p v-if="data.purpose" class="responsibility-purpose">{{ data.purpose }}</p>
    <div v-if="data.inputs?.length" class="responsibility-port-group">
      <span class="port-label">输入</span>
      <span v-for="input in data.inputs" :key="input" class="port-chip input-chip">{{ input }}</span>
    </div>
    <div v-if="data.outputs?.length" class="responsibility-port-group">
      <span class="port-label">输出</span>
      <span v-for="output in data.outputs" :key="output" class="port-chip output-chip">{{ output }}</span>
    </div>
    <div v-if="data.requiredCapabilities?.length" class="capability-group">
      <span v-for="capability in data.requiredCapabilities" :key="capability" class="capability-chip">{{ capability }}</span>
    </div>
    <Handle type="source" :position="Position.Bottom" />
  </div>
</template>

<script setup>
import { Handle, Position } from '@vue-flow/core'

defineProps({ data: { type: Object, required: true } })
</script>

<style scoped>
.creator-responsibility-node { box-sizing: border-box; width: 250px; min-height: 150px; padding: 12px; border: 1px solid #bfdbfe; border-radius: 14px; background: #fff; box-shadow: 0 12px 30px rgba(37, 99, 235, .12); font-size: 12px; color: #0f172a; }
.responsibility-node-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
.responsibility-node-header strong { font-family: 'Fira Code', 'Cascadia Code', monospace; word-break: break-all; }
.role-badge { padding: 2px 8px; border-radius: 999px; background: #dbeafe; color: #1d4ed8; white-space: nowrap; font-size: 11px; }
.responsibility-purpose { margin: 0 0 8px; color: #475569; line-height: 1.35; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.responsibility-port-group, .capability-group { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; align-items: center; }
.port-label { font-weight: 700; color: #64748b; }
.port-chip, .capability-chip { padding: 2px 6px; border-radius: 999px; font-family: 'Fira Code', 'Cascadia Code', monospace; font-size: 11px; }
.input-chip { background: #ecfeff; color: #0e7490; }
.output-chip { background: #f0fdf4; color: #15803d; }
.capability-chip { background: #fef3c7; color: #92400e; }
</style>
