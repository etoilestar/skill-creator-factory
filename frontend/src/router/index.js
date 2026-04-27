import { createRouter, createWebHistory } from 'vue-router'
import CreatorView from '../views/CreatorView.vue'
import SkillsView from '../views/SkillsView.vue'
import SandboxView from '../views/SandboxView.vue'

const routes = [
  { path: '/', redirect: '/creator' },
  { path: '/creator', component: CreatorView },
  { path: '/skills', component: SkillsView },
  { path: '/sandbox', component: SandboxView },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
