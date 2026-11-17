<template>
  <el-container class="layout">
    <el-header class="header">
      <div class="brand" @click="$router.push({ name: 'projects' })">
        <el-icon :size="22"><Cpu /></el-icon>
        <span>图片三维重建系统</span>
      </div>
      <div class="right">
        <span class="user">{{ auth.user?.username }}</span>
        <el-button text @click="onLogout">退出登录</el-button>
      </div>
    </el-header>
    <el-main class="main">
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'
import { Cpu } from '@element-plus/icons-vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()

function onLogout() {
  auth.logout()
  router.push({ name: 'login' })
}
</script>

<style scoped>
.layout {
  height: 100vh;
  display: flex;
  flex-direction: column;
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #1f2d3d;
  color: #fff;
  height: 56px;
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 17px;
  font-weight: 600;
  cursor: pointer;
}
.right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.right .user {
  color: #cfd8e3;
  font-size: 14px;
}
.right :deep(.el-button) {
  color: #fff;
}
.main {
  padding: 20px;
  overflow: auto;
}
</style>
