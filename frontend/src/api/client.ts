import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '../router'
import { useAuthStore } from '../stores/auth'

export const api = axios.create({
  baseURL: '/api/v1',
  timeout: 120000,
})

api.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.token) {
    config.headers.Authorization = `Bearer ${auth.token}`
  }
  return config
})

api.interceptors.response.use(
  (resp) => resp,
  (error) => {
    const status = error.response?.status
    if (status === 401) {
      const auth = useAuthStore()
      auth.logout()
      if (router.currentRoute.value.name !== 'login') {
        ElMessage.error('登录已过期，请重新登录')
        router.push({ name: 'login' })
      }
    }
    return Promise.reject(error)
  },
)

export function errMsg(error: any): string {
  const data = error?.response?.data
  if (data?.message) return data.message
  if (typeof data?.detail === 'string') return data.detail
  if (Array.isArray(data?.detail)) {
    return data.detail.map((d: any) => d.msg).join('; ')
  }
  return error?.message || '请求失败'
}
