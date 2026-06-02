import { ref, computed } from 'vue'

const API_BASE = '/api/publish'

export function usePublish() {
  const configs = ref([])
  const availableSkills = ref([])
  const loading = ref(false)
  const error = ref('')

  async function fetchConfigs() {
    loading.value = true
    error.value = ''
    try {
      const res = await fetch(`${API_BASE}/configs`)
      const data = await res.json()
      configs.value = data.configs || []
    } catch (e) {
      error.value = e.message
    } finally {
      loading.value = false
    }
  }

  async function fetchAvailableSkills() {
    try {
      const res = await fetch(`${API_BASE}/available-skills`)
      const data = await res.json()
      availableSkills.value = data.skills || []
    } catch (e) {
      error.value = e.message
    }
  }

  async function createConfig(payload) {
    const res = await fetch(`${API_BASE}/configs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!res.ok) throw new Error('Failed to create config')
    const config = await res.json()
    configs.value.push(config)
    return config
  }

  async function updateConfig(endpointId, payload) {
    const res = await fetch(`${API_BASE}/configs/${endpointId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!res.ok) throw new Error('Failed to update config')
    const updated = await res.json()
    const idx = configs.value.findIndex(c => c.endpoint_id === endpointId)
    if (idx !== -1) configs.value[idx] = updated
    return updated
  }

  async function deleteConfig(endpointId) {
    const res = await fetch(`${API_BASE}/configs/${endpointId}`, {
      method: 'DELETE',
    })
    if (!res.ok) throw new Error('Failed to delete config')
    configs.value = configs.value.filter(c => c.endpoint_id !== endpointId)
  }

  async function toggleConfig(endpointId) {
    const res = await fetch(`${API_BASE}/configs/${endpointId}/toggle`, {
      method: 'POST',
    })
    if (!res.ok) throw new Error('Failed to toggle config')
    const updated = await res.json()
    const idx = configs.value.findIndex(c => c.endpoint_id === endpointId)
    if (idx !== -1) configs.value[idx] = updated
    return updated
  }

  return {
    configs,
    availableSkills,
    loading,
    error,
    fetchConfigs,
    fetchAvailableSkills,
    createConfig,
    updateConfig,
    deleteConfig,
    toggleConfig,
  }
}
