<template><div class="attachments"><span v-for="(file, i) in files" :key="file.path">{{ icon(file) }} {{ file.filename }} · {{ file.mime_type || 'application/octet-stream' }} · {{ size(file.size) }} <button @click="$emit('remove', i)" :disabled="disabled">✕</button></span></div></template>
<script setup>
defineProps({ files: { type: Array, default: () => [] }, disabled: Boolean })
defineEmits(['remove'])
const icon = f => ({ image: '🖼️', audio: '🔊', video: '🎬' }[String(f.mime_type || '').split('/')[0]] || '📄')
const size = n => n < 1024 ? `${n || 0} B` : `${(n / 1024).toFixed(1)} KB`
</script>
<style scoped>.attachments{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}.attachments span{padding:4px 8px;border:1px solid var(--border);border-radius:8px;font-size:12px}</style>
