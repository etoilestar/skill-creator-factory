import { createRouter, createWebHistory } from 'vue-router'
import CreatorView from '../views/CreatorView.vue'
import SkillsView from '../views/SkillsView.vue'
import SandboxView from '../views/SandboxView.vue'
import PublishView from '../views/PublishView.vue'

const routes = [
  { path: '/', redirect: '/creator' },
  { path: '/creator', component: CreatorView },
  { path: '/skills', component: SkillsView },
  { path: '/sandbox', component: SandboxView },
  { path: '/publish', component: PublishView },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
