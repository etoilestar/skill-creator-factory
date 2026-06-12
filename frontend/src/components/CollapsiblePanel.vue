<template>
  <section class="collapsible-panel card" :class="statusClass">
    <button class="panel-header" type="button" @click="toggle">
      <span class="panel-copy">
        <span class="panel-title"><slot name="title">{{ title }}</slot></span>
        <small v-if="description" class="panel-description">{{ description }}</small>
      </span>
      <span v-if="badge" class="panel-badge">{{ badge }}</span>
      <span v-if="status" class="panel-status">{{ status }}</span>
      <span class="panel-chevron" :class="{ open: model }">⌄</span>
    </button>
    <div v-show="model" class="panel-body">
      <slot />
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'
const props = defineProps({
  title: { type: String, default: '' },
  description: { type: String, default: '' },
  badge: { type: [String, Number], default: '' },
  status: { type: String, default: '' },
})
const model = defineModel('open', { type: Boolean, default: false })
const statusClass = computed(() => props.status ? `status-${props.status}` : '')
function toggle() { model.value = !model.value }
</script>

<style scoped>
.collapsible-panel { padding: 0; overflow: hidden; }
.panel-header { width: 100%; display: flex; align-items: center; gap: 12px; justify-content: space-between; padding: 16px 18px; border: 0; background: transparent; color: var(--text); cursor: pointer; text-align: left; }
.panel-copy { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.panel-title { font-weight: 700; font-size: 16px; }
.panel-description { color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.panel-badge, .panel-status { color: var(--text-muted); border: 1px solid var(--border); border-radius: 999px; padding: 2px 8px; font-size: 12px; margin-left: auto; white-space: nowrap; }
.panel-status { margin-left: 0; text-transform: uppercase; }
.status-ok { border-color: color-mix(in srgb, var(--success) 45%, var(--border)); }
.status-bad { border-color: color-mix(in srgb, var(--danger) 45%, var(--border)); }
.panel-chevron { color: var(--text-muted); transition: transform .15s ease; font-size: 20px; }
.panel-chevron.open { transform: rotate(180deg); }
.panel-body { padding: 0 18px 18px; }
</style>
