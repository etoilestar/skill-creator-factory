<template>
  <section v-if="manifest" class="sandbox-results">
    <details v-for="(item, index) in manifest.structured_outputs || []" :key="index">
      <summary>结构化输出 {{ index + 1 }}</summary><pre>{{ JSON.stringify(item.data, null, 2) }}</pre>
    </details>
    <article v-for="artifact in manifest.artifacts || []" :key="artifact.path" class="artifact">
      <img v-if="family(artifact) === 'image'" :src="artifact.url" :alt="artifact.name">
      <audio v-else-if="family(artifact) === 'audio'" :src="artifact.url" controls />
      <video v-else-if="family(artifact) === 'video'" :src="artifact.url" controls />
      <div><strong>{{ artifact.name }}</strong> · {{ artifact.mime_type }} · {{ formatSize(artifact.size) }}</div>
      <a :href="artifact.url" :download="artifact.name">打开 / 下载</a>
    </article>
  </section>
</template>
<script setup>
defineProps({ manifest: { type: Object, default: null } })
const family = artifact => String(artifact.mime_type || '').split('/')[0]
const formatSize = size => size < 1024 ? `${size || 0} B` : `${(size / 1024).toFixed(1)} KB`
</script>
<style scoped>
.sandbox-results{display:grid;gap:10px;padding:10px;border:1px solid var(--border);border-radius:8px}.artifact{display:grid;gap:6px}.artifact img,.artifact video{max-width:100%;max-height:360px}.artifact audio{width:100%}pre{overflow:auto;white-space:pre-wrap}
</style>
