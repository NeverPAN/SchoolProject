<template>
  <div class="login-bg">
    <el-card class="login-card">
      <h2 class="title">图片三维重建系统</h2>
      <p class="subtitle">多视角图片 → 三维模型</p>
      <el-tabs v-model="tab">
        <el-tab-pane label="登录" name="login">
          <el-form @submit.prevent="onLogin" label-position="top">
            <el-form-item label="用户名">
              <el-input v-model="form.username" placeholder="请输入用户名" data-testid="login-username" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="form.password" type="password" show-password placeholder="请输入密码" data-testid="login-password" @keyup.enter="onLogin" />
            </el-form-item>
            <el-button type="primary" class="submit" :loading="loading" data-testid="login-submit" @click="onLogin">登 录</el-button>
          </el-form>
        </el-tab-pane>
        <el-tab-pane label="注册" name="register">
          <el-form label-position="top">
            <el-form-item label="用户名（≥3 位）">
              <el-input v-model="form.username" placeholder="字母/数字/中文" data-testid="register-username" />
            </el-form-item>
            <el-form-item label="密码（≥6 位）">
              <el-input v-model="form.password" type="password" show-password data-testid="register-password" @keyup.enter="onRegister" />
            </el-form-item>
            <el-button type="primary" class="submit" :loading="loading" data-testid="register-submit" @click="onRegister">注 册</el-button>
          </el-form>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../../stores/auth'
import { errMsg } from '../../api'

const auth = useAuthStore()
const router = useRouter()
const tab = ref('login')
const loading = ref(false)
const form = reactive({ username: '', password: '' })

async function onLogin() {
  if (!form.username || !form.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    ElMessage.success('登录成功')
    router.push({ name: 'projects' })
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    loading.value = false
  }
}

async function onRegister() {
  loading.value = true
  try {
    await auth.register(form.username, form.password)
    ElMessage.success('注册成功，请登录')
    tab.value = 'login'
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-bg {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1f2d3d 0%, #2d5a88 100%);
}
.login-card {
  width: 380px;
  padding: 8px 12px;
}
.title {
  text-align: center;
  margin: 8px 0 4px;
  color: #1f2d3d;
}
.subtitle {
  text-align: center;
  color: #909399;
  font-size: 13px;
  margin: 0 0 8px;
}
.submit {
  width: 100%;
}
</style>
