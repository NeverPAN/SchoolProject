<template>
  <div>
    <div class="toolbar">
      <h3>我的项目</h3>
      <div class="spacer" />
      <el-input
        v-model="keyword"
        placeholder="搜索项目名称"
        clearable
        style="width: 220px"
        :prefix-icon="Search"
      />
      <el-button type="primary" :icon="Plus" @click="dialogVisible = true">新建项目</el-button>
    </div>

    <el-table :data="filtered" v-loading="loading" stripe>
      <el-table-column label="项目名称" min-width="220">
        <template #default="{ row }">
          <el-link type="primary" @click="$router.push({ name: 'project-detail', params: { id: row.id } })">
            {{ row.name }}
          </el-link>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="描述" min-width="220" show-overflow-tooltip />
      <el-table-column label="图片数" width="90" align="center">
        <template #default="{ row }">{{ row.image_count }}</template>
      </el-table-column>
      <el-table-column label="总大小" width="110" align="center">
        <template #default="{ row }">{{ fmtSize(row.total_image_bytes) }}</template>
      </el-table-column>
      <el-table-column label="任务数" width="90" align="center">
        <template #default="{ row }">{{ row.job_count }}</template>
      </el-table-column>
      <el-table-column label="模型数" width="90" align="center">
        <template #default="{ row }">{{ row.model_count }}</template>
      </el-table-column>
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150" align="center">
        <template #default="{ row }">
          <el-button size="small" @click="$router.push({ name: 'project-detail', params: { id: row.id } })">打开</el-button>
          <el-button size="small" type="danger" plain @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogVisible" title="新建项目" width="440px">
      <el-form label-width="70px">
        <el-form-item label="名称">
          <el-input v-model="newName" placeholder="例如：文物数字化" data-testid="new-project-name" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="newDesc" type="textarea" :rows="2" placeholder="项目说明（可选）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" data-testid="create-project-btn" @click="onCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search } from '@element-plus/icons-vue'
import { projectApi, fmtSize, fmtTime, errMsg, type Project } from '../../api'

const projects = ref<Project[]>([])
const loading = ref(false)
const keyword = ref('')
const dialogVisible = ref(false)
const creating = ref(false)
const newName = ref('')
const newDesc = ref('')

const filtered = computed(() =>
  projects.value.filter((p) => p.name.toLowerCase().includes(keyword.value.toLowerCase())),
)

async function load() {
  loading.value = true
  try {
    projects.value = await projectApi.list()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    loading.value = false
  }
}

async function onCreate() {
  if (!newName.value.trim()) {
    ElMessage.warning('请输入项目名称')
    return
  }
  creating.value = true
  try {
    await projectApi.create(newName.value.trim(), newDesc.value)
    ElMessage.success('项目已创建')
    dialogVisible.value = false
    newName.value = ''
    newDesc.value = ''
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    creating.value = false
  }
}

async function onDelete(p: Project) {
  try {
    await ElMessageBox.confirm(`确定删除项目「${p.name}」？项目的图片、任务与模型记录将一并删除。`, '删除确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    await projectApi.remove(p.id)
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

onMounted(load)
</script>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.toolbar h3 {
  margin: 0;
}
.spacer {
  flex: 1;
}
</style>
