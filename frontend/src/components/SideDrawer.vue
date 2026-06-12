<template>
  <Teleport to="body">
    <div v-if="open" class="drawer-backdrop" @click="close">
      <aside class="side-drawer" @click.stop>
        <header class="drawer-header">
          <div>
            <p v-if="eyebrow" class="eyebrow">{{ eyebrow }}</p>
            <h2>{{ title }}</h2>
            <p v-if="description" class="muted small">{{ description }}</p>
          </div>
          <button class="btn-ghost" type="button" @click="close">关闭</button>
        </header>
        <div class="drawer-body">
          <slot />
        </div>
      </aside>
    </div>
  </Teleport>
</template>

<script setup>
defineProps({ title: { type: String, default: '' }, description: { type: String, default: '' }, eyebrow: { type: String, default: '' } })
const open = defineModel('open', { type: Boolean, default: false })
function close() { open.value = false }
</script>

<style scoped>
.drawer-backdrop { position: fixed; inset: 0; z-index: 80; background: rgba(2, 6, 23, .55); display: flex; justify-content: flex-end; }
.side-drawer { width: min(760px, 92vw); height: 100%; background: var(--surface); border-left: 1px solid var(--border); box-shadow: -18px 0 50px rgba(0,0,0,.35); display: flex; flex-direction: column; }
.drawer-header { padding: 20px 22px; border-bottom: 1px solid var(--border); display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.drawer-header h2 { margin: 0; }
.drawer-body { padding: 20px 22px; overflow: auto; display: flex; flex-direction: column; gap: 18px; }
.eyebrow { color: var(--accent); text-transform: uppercase; letter-spacing: .08em; font-size: 12px; margin: 0 0 4px; }
.small { font-size: 12px; }
@media (max-width: 680px) { .side-drawer { width: 100vw; } .drawer-header { flex-direction: column; } }
</style>
