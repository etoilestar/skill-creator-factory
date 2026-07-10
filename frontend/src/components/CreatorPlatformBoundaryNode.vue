<template>
  <div class="creator-platform-boundary-node" :class="data.kind">
    <Handle type="target" :position="Position.Top" />
    <div class="boundary-label">{{ data.label }}</div>
    <code>{{ data.nodeId }}</code>
    <Handle type="source" :position="Position.Bottom" />
  </div>
</template>

<script setup>
import { Handle, Position } from '@vue-flow/core'

defineProps({ data: { type: Object, required: true } })
</script>

<style scoped>
.creator-platform-boundary-node { width: 220px; padding: 16px; border: 2px dashed #94a3b8; border-radius: 16px; background: #f8fafc; text-align: center; color: #334155; box-shadow: 0 10px 24px rgba(15,23,42,.08); }
.creator-platform-boundary-node.input { border-color: #38bdf8; background: #ecfeff; }
.creator-platform-boundary-node.output { border-color: #a78bfa; background: #f5f3ff; }
.boundary-label { font-weight: 800; margin-bottom: 6px; }
code { font-size: 12px; word-break: break-all; }
</style>
