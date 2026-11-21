import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('../views/login/LoginView.vue') },
    {
      path: '/',
      component: () => import('../layouts/MainLayout.vue'),
      children: [
        { path: '', name: 'projects', component: () => import('../views/projects/ProjectsView.vue') },
        {
          path: 'projects/:id',
          name: 'project-detail',
          component: () => import('../views/projects/ProjectDetailView.vue'),
          props: true,
        },
        {
          path: 'jobs/:id',
          name: 'job-detail',
          component: () => import('../views/jobs/JobDetailView.vue'),
          props: true,
        },
        {
          path: 'models/:id',
          name: 'model-viewer',
          component: () => import('../views/models/ModelViewerView.vue'),
          props: true,
        },
      ],
    },
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.name !== 'login' && !auth.isLoggedIn) {
    return { name: 'login' }
  }
  if (to.name === 'login' && auth.isLoggedIn) {
    return { name: 'projects' }
  }
})

export default router
