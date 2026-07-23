<template>
  <section class="page">
    <h1>Creator 模型配置</h1>
    <p class="muted">保存后下一次 Creator Planner 或 Reviewer 调用立即使用新配置。留空的字段继续使用全局默认配置。</p>
    <div class="cards">
      <form v-for="role in roles" :key="role.id" class="card" @submit.prevent="save(role.id)">
        <h2>{{ role.label }}</h2>
        <label>API Base URL<input v-model="forms[role.id].base_url" placeholder="使用全局默认" /></label>
        <label>API Key<input v-model="forms[role.id].api_key" type="password" autocomplete="new-password" :placeholder="configured[role.id] ? '已配置；留空则保持不变' : '使用全局默认'" /></label>
        <label>Model Name<input v-model="forms[role.id].model" placeholder="使用全局默认" /></label>
        <label>Max Output Tokens<input v-model="forms[role.id].max_tokens" type="number" min="1" placeholder="使用全局默认" /></label>
        <p v-if="configured[role.id]" class="configured">API Key：已配置</p>
        <div class="actions"><button class="btn-primary" :disabled="busy">保存</button><button type="button" class="btn-ghost" :disabled="busy" @click="restore(role.id)">恢复全局默认</button></div>
      </form>
    </div>
    <p v-if="message" class="notice">{{ message }}</p>
  </section>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
const roles = [{ id: 'planner', label: 'Planner' }, { id: 'reviewer', label: 'Reviewer' }]
const forms = reactive({ planner: { base_url: '', api_key: '', model: '', max_tokens: '' }, reviewer: { base_url: '', api_key: '', model: '', max_tokens: '' } })
const configured = reactive({ planner: false, reviewer: false })
const busy = ref(false); const message = ref('')
async function load() {
  const response = await fetch('/api/creator/model-profiles'); const data = await response.json()
  for (const role of roles) { const profile = data.profiles[role.id]; Object.assign(forms[role.id], { base_url: profile.base_url || '', api_key: '', model: profile.model || '', max_tokens: profile.max_tokens ?? '' }); configured[role.id] = profile.api_key_configured }
}
async function update(role, body) { busy.value = true; message.value = ''; try { const response = await fetch(`/api/creator/model-profiles/${role}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); const data = await response.json(); if (!response.ok) throw new Error(data.detail || '保存失败'); message.value = '配置已保存，下一次调用立即生效。'; await load() } catch (error) { message.value = error.message } finally { busy.value = false } }
function save(role) { const form = forms[role]; const body = { base_url: form.base_url || '', model: form.model || '', max_tokens: form.max_tokens === '' ? null : Number(form.max_tokens) }; if (form.api_key) body.api_key = form.api_key; return update(role, body) }
function restore(role) { return update(role, { restore_defaults: true }) }
onMounted(load)
</script>

<style scoped>
.page{padding:28px;overflow:auto}.muted{color:var(--text-muted)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin-top:24px}.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px}.card label{display:grid;gap:6px;margin:14px 0;font-size:13px}.card input{padding:9px;border:1px solid var(--border);border-radius:6px;background:var(--surface2);color:var(--text)}.actions{display:flex;gap:8px;margin-top:18px}.configured,.notice{color:var(--success);font-size:13px}
</style>
