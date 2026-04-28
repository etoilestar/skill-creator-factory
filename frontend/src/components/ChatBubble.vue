<template>
  <div class="bubble-content">
    <template v-for="(seg, idx) in segments" :key="idx">
      <!-- Think block -->
      <div v-if="seg.type === 'think'" class="think-block">
        <button class="think-toggle" @click="toggleThink(idx)" type="button">
          <span class="think-icon">💭</span>
          <span class="think-label">思考过程</span>
          <span v-if="seg.open" class="think-streaming">…</span>
          <span class="think-chevron">{{ collapsed[idx] ? '▶' : '▼' }}</span>
        </button>
        <pre v-if="!collapsed[idx]" class="think-body">{{ seg.content }}<span v-if="seg.open && streaming" class="cursor">▋</span></pre>
      </div>
      <!-- Normal text -->
      <pre v-else-if="seg.content" class="content">{{ seg.content }}<span v-if="streaming && idx === segments.length - 1" class="cursor">▋</span></pre>
    </template>
  </div>
</template>

<script setup>
import { computed, reactive } from 'vue'

const props = defineProps({
  content: { type: String, required: true },
  streaming: { type: Boolean, default: false },
})

/**
 * Parse content into segments of type 'think' or 'text'.
 * Handles incomplete (open) think tags during streaming.
 */
function parseSegments(text) {
  const segments = []
  const closeTag = '</think>'
  const openTag = '<think>'
  let remaining = text

  while (remaining.length > 0) {
    const openIdx = remaining.indexOf(openTag)
    if (openIdx === -1) {
      segments.push({ type: 'text', content: remaining })
      break
    }
    if (openIdx > 0) {
      segments.push({ type: 'text', content: remaining.slice(0, openIdx) })
    }
    remaining = remaining.slice(openIdx + openTag.length)
    const closeIdx = remaining.indexOf(closeTag)
    if (closeIdx === -1) {
      // Unclosed think block (still streaming)
      segments.push({ type: 'think', content: remaining, open: true })
      remaining = ''
    } else {
      segments.push({ type: 'think', content: remaining.slice(0, closeIdx), open: false })
      remaining = remaining.slice(closeIdx + closeTag.length)
    }
  }

  return segments
}

const segments = computed(() => parseSegments(props.content))

// Track collapsed state per segment index
const collapsed = reactive({})
// Track which completed think blocks have been auto-collapsed
const autoCollapsed = reactive({})

// Auto-collapse completed think blocks on first completion
computed(() => {
  segments.value.forEach((seg, idx) => {
    if (seg.type === 'think' && !seg.open && !autoCollapsed[idx]) {
      autoCollapsed[idx] = true
      collapsed[idx] = true
    }
  })
  return null
}).value

function toggleThink(idx) {
  collapsed[idx] = !collapsed[idx]
}
</script>

<style scoped>
.bubble-content {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
}

.content {
  font-family: var(--font);
  font-size: 14px;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}

/* Think block */
.think-block {
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}

.think-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 6px 10px;
  background: transparent;
  border: none;
  cursor: pointer;
  font-size: 12px;
  color: var(--text-muted);
  text-align: left;
  transition: background 0.15s;
}
.think-toggle:hover {
  background: var(--surface2);
}

.think-icon { font-size: 13px; }
.think-label { font-weight: 500; }
.think-streaming {
  font-style: italic;
  opacity: 0.7;
}
.think-chevron {
  margin-left: auto;
  font-size: 10px;
  opacity: 0.6;
}

.think-body {
  font-family: var(--font);
  font-size: 12px;
  color: var(--text-muted);
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
  padding: 8px 10px;
  border-top: 1px solid var(--border);
  font-style: italic;
  max-height: 400px;
  overflow-y: auto;
}

.cursor { animation: blink 1s step-end infinite; }
@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }
</style>
