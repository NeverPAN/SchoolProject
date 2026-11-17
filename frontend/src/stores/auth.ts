import { defineStore } from 'pinia'
import { authApi, type User } from '../api'

const TOKEN_KEY = 'recon_token'
const USER_KEY = 'recon_user'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem(TOKEN_KEY) || '',
    user: JSON.parse(localStorage.getItem(USER_KEY) || 'null') as User | null,
  }),
  getters: {
    isLoggedIn: (s) => !!s.token,
  },
  actions: {
    async login(username: string, password: string) {
      const r = await authApi.login(username, password)
      this.token = r.access_token
      this.user = r.user
      localStorage.setItem(TOKEN_KEY, r.access_token)
      localStorage.setItem(USER_KEY, JSON.stringify(r.user))
    },
    async register(username: string, password: string) {
      await authApi.register(username, password)
    },
    async refreshMe() {
      this.user = await authApi.me()
      localStorage.setItem(USER_KEY, JSON.stringify(this.user))
    },
    logout() {
      this.token = ''
      this.user = null
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(USER_KEY)
    },
  },
})
