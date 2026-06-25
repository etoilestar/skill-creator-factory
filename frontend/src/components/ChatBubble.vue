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
      <!-- Normal text with citation rendering -->
      <pre v-else-if="seg.content" class="content" v-html="renderCitations(seg.content) + (streaming && idx === segments.length - 1 ? '<span class=\'cursor\'>▋</span>' : '')"></pre>
    </template>
    <div v-if="files && files.length" class="file-attachments">
      <span v-for="f in files" :key="f.filename" class="file-chip">📎 {{ f.filename }}</span>
    </div>
    <!-- Citation detail panel -->
    <div v-if="expandedCitation" class="citation-detail">
      <div class="citation-detail-header">
        <span class="citation-detail-title">引用来源</span>
        <button class="citation-detail-close" @click="expandedCitation = null" type="button">✕</button>
      </div>
      <div class="citation-detail-body">{{ expandedCitation }}</div>
    </div>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watchEffect } from 'vue'

const props = defineProps({
  content: { type: String, required: true },
  streaming: { type: Boolean, default: false },
  files: { type: Array, default: undefined },
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
watchEffect(() => {
  segments.value.forEach((seg, idx) => {
    if (seg.type === 'think' && !seg.open && !autoCollapsed[idx]) {
      autoCollapsed[idx] = true
      collapsed[idx] = true
    }
  })
})

function toggleThink(idx) {
  collapsed[idx] = !collapsed[idx]
}

// --- Citation rendering ---
const expandedCitation = ref(null)

/**
 * Render citation tags in text content.
 * Matches patterns like [来源：xxx.pdf 第3页] or [引用：xxx.docx] or [出处：xxx]
 * and converts them to clickable blue tags.
 */
function renderCitations(text) {
  if (!text) return ''
  // Escape HTML first
  const escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  // Replace citation patterns with clickable tags
  return escaped.replace(
    /\[(来源|引用|出处)[：:]\s*([^\]]+)\]/g,
    (match, label, detail) => {
      return `<span class="citation-tag" data-detail="${encodeURIComponent(detail)}" onclick="this.dispatchEvent(new CustomEvent('citation-click',{bubbles:true,detail:decodeURIComponent('${encodeURIComponent(detail)}')}))">${label}：${detail}</span>`
    }
  )
}

// Listen for citation click events (delegated from v-html)
function handleCitationClick(e) {
  const detail = e.detail
  if (detail) {
    expandedCitation.value = expandedCitation.value === detail ? null : detail
  }
}

// Attach/detach event listener on mount
import { onMounted, onUnmounted } from 'vue'
onMounted(() => {
  document.addEventListener('citation-click', handleCitationClick)
})
onUnmounted(() => {
  document.removeEventListener('citation-click', handleCitationClick)
})
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

.file-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 4px;
}
.file-chip {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 10px;
  background: var(--surface2, #f0f0f0);
  color: var(--text-muted, #666);
  white-space: nowrap;
}

/* Citation tag */
:deep(.citation-tag) {
  display: inline;
  font-size: 12px;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(59, 130, 246, 0.12);
  color: #3b82f6;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s;
  font-family: var(--font);
}
:deep(.citation-tag:hover) {
  background: rgba(59, 130, 246, 0.22);
}

/* Citation detail panel */
.citation-detail {
  margin-top: 4px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface2, #f8f9fa);
  font-size: 12px;
}
.citation-detail-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
}
.citation-detail-title {
  font-weight: 600;
  color: var(--text-muted);
}
.citation-detail-close {
  background: none;
  border: none;
  cursor: pointer;
  font-size: 12px;
  color: var(--text-muted);
  padding: 0 2px;
}
.citation-detail-body {
  color: var(--text, #333);
  white-space: pre-wrap;
  word-break: break-word;
}
</style>