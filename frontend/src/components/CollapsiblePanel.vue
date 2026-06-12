<template>
  <section class="collapsible-panel card">
    <button class="panel-header" type="button" @click="toggle">
      <span class="panel-title"><slot name="title">{{ title }}</slot></span>
      <span v-if="badge" class="panel-badge">{{ badge }}</span>
      <span class="panel-chevron" :class="{ open: model }">⌄</span>
    </button>
    <div v-show="model" class="panel-body">
      <slot />
    </div>
  </section>
</template>

<script setup>
defineProps({ title: { type: String, default: '' }, badge: { type: [String, Number], default: '' } })
const model = defineModel('open', { type: Boolean, default: false })
function toggle() { model.value = !model.value }
</script>

<style scoped>
.collapsible-panel { padding: 0; overflow: hidden; }
.panel-header { width: 100%; display: flex; align-items: center; gap: 12px; justify-content: space-between; padding: 16px 18px; border: 0; background: transparent; color: var(--text); cursor: pointer; text-align: left; }
.panel-title { font-weight: 700; font-size: 16px; }
.panel-badge { color: var(--text-muted); border: 1px solid var(--border); border-radius: 999px; padding: 2px 8px; font-size: 12px; margin-left: auto; }
.panel-chevron { color: var(--text-muted); transition: transform .15s ease; font-size: 20px; }
.panel-chevron.open { transform: rotate(180deg); }
.panel-body { padding: 0 18px 18px; }
</style>
