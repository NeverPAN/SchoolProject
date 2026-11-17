<template>
  <div>
    <div class="toolbar">
      <el-button :icon="ArrowLeft" text @click="$router.back()">返回</el-button>
      <h3>三维模型查看 #{{ id }}</h3>
      <div class="spacer" />
      <el-button :icon="Refresh" @click="viewer?.resetView()">重置视角</el-button>
      <el-button type="primary" :icon="Download" @click="onDownload">下载模型</el-button>
    </div>

    <el-row :gutter="16">
      <el-col :span="19">
        <div class="viewer-wrap">
          <ModelViewer
            ref="viewer"
            :file-data="fileData"
            :file-format="model?.format || 'glb'"
            :style="stylePreset"
            :background="bgPreset"
            @loaded="onLoaded"
          />
        </div>
      </el-col>
      <el-col :span="5">
        <el-card shadow="never" class="panel">
          <template #header><span>展示风格</span></template>
          <div class="style-grid">
            <el-button
              v-for="s in styles"
              :key="s.key"
              :type="stylePreset === s.key ? 'primary' : 'default'"
              size="small"
              @click="stylePreset = s.key"
            >{{ s.label }}</el-button>
          </div>
          <el-divider />
          <span class="label">背景</span>
          <el-radio-group v-model="bgPreset" size="small" style="margin-top: 8px">
            <el-radio-button value="light">浅色</el-radio-button>
            <el-radio-button value="gradient">渐变</el-radio-button>
            <el-radio-button value="dark">深色</el-radio-button>
          </el-radio-group>
          <el-divider />
          <template v-if="model">
            <el-descriptions :column="1" size="small" border>
              <el-descriptions-item label="类型">{{ modelTypeLabel(model.model_type) }}</el-descriptions-item>
              <el-descriptions-item label="格式">{{ model.format.toUpperCase() }}</el-descriptions-item>
              <el-descriptions-item label="文件大小">{{ fmtSize(model.file_size) }}</el-descriptions-item>
              <el-descriptions-item label="顶点数">{{ counts.vertices.toLocaleString() || model.vertex_count?.toLocaleString() || '-' }}</el-descriptions-item>
              <el-descriptions-item label="面数">{{ counts.faces.toLocaleString() || model.face_count?.toLocaleString() || '-' }}</el-descriptions-item>
              <el-descriptions-item label="点数">{{ counts.points.toLocaleString() || model.point_count?.toLocaleString() || '-' }}</el-descriptions-item>
              <el-descriptions-item label="包围盒">{{ info.sizeLabel || '-' }}</el-descriptions-item>
              <el-descriptions-item label="生成时间">{{ fmtTime(model.created_at) }}</el-descriptions-item>
            </el-descriptions>
            <el-divider />
            <span class="label">模型版本</span>
            <el-timeline style="margin-top: 12px">
              <el-timeline-item
                v-for="v in model.versions || []"
                :key="v.id"
                :timestamp="fmtTime(v.created_at)"
                type="primary"
              >
                v{{ v.version_number }} · {{ versionLabel(v.change_type) }}
                <div class="ver-desc">{{ v.change_description }}</div>
              </el-timeline-item>
            </el-timeline>
          </template>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowLeft, Download, Refresh } from '@element-plus/icons-vue'
import ModelViewer, { type StylePreset, type BackgroundPreset } from '../../three/ModelViewer.vue'
import { modelApi, fmtSize, fmtTime, errMsg, type ModelItem } from '../../api'

const props = defineProps<{ id: string }>()
const viewer = ref<InstanceType<typeof ModelViewer> | null>(null)
const model = ref<ModelItem | null>(null)
const fileData = ref<ArrayBuffer | null>(null)
const stylePreset = ref<StylePreset>('original')
const bgPreset = ref<BackgroundPreset>('gradient')
const counts = ref({ vertices: 0, faces: 0, points: 0 })
const info = ref({ sizeLabel: '' })

const styles: { key: StylePreset; label: string }[] = [
  { key: 'original', label: '写实' },
  { key: 'morandi', label: '莫兰迪' },
  { key: 'monochrome', label: '单色' },
  { key: 'wireframe', label: '线框' },
  { key: 'pointcloud', label: '点云' },
]

function modelTypeLabel(t: string) {
  return { sparse_pointcloud: '稀疏点云', dense_pointcloud: '稠密点云', mesh: '网格', web: 'Web 展示' }[t] || t
}
function versionLabel(t: string) {
  return { initial: '首次重建', append_images: '追加照片更新', manual: '手动版本' }[t] || t
}

function onLoaded(i: { vertices: number; faces: number; points: number; sizeLabel: string }) {
  counts.value = { vertices: i.vertices, faces: i.faces, points: i.points }
  info.value = { sizeLabel: i.sizeLabel }
}

async function onDownload() {
  try {
    await modelApi.download(Number(props.id))
    ElMessage.success('下载已开始')
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

onMounted(async () => {
  try {
    model.value = await modelApi.get(Number(props.id))
    // 深色格式（glb）用写实风格；ply 点云默认点云风格
    if (model.value.format === 'ply' && model.value.model_type.includes('pointcloud')) {
      stylePreset.value = 'pointcloud'
    }
    fileData.value = await modelApi.fetchFile(Number(props.id))
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
})
</script>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
.toolbar h3 { margin: 0; }
.spacer { flex: 1; }
.viewer-wrap { height: calc(100vh - 160px); min-height: 480px; }
.panel { height: calc(100vh - 160px); overflow: auto; }
.style-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
.style-grid .el-button { margin: 0; width: 100%; }
.label { font-size: 13px; color: #606266; font-weight: 600; }
.ver-desc { font-size: 11px; color: #909399; }
</style>
