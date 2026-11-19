<template>
  <div>
    <div class="toolbar">
      <el-button :icon="ArrowLeft" text @click="$router.back()">返回</el-button>
      <h3>三维模型查看 #{{ id }}</h3>
      <el-tag v-if="model && isPhotoTexture(model)" type="success" size="small">照片贴图 · GLB</el-tag>
      <div class="spacer" />
      <el-button :icon="Refresh" :disabled="!info.sizeLabel" @click="viewer?.resetView()">重置视角</el-button>
      <el-button type="primary" :icon="Download" @click="onDownload">下载模型</el-button>
    </div>

    <div class="viewer-layout">
      <div class="viewer-wrap">
          <ModelViewer
          :select-mode="selectMode"
          @region-selected="onRegionSelected"
          @region-failed="(r: string) => ElMessage.warning(r)
"            ref="viewer"
            :file-data="fileData"
            :pending="pending"
            :fetch-error="fetchError"
            :display-up="model?.display_up"
            :legacy-srgb-vertex-colors="model?.legacy_srgb_vertex_colors"
            :file-format="model?.format || 'glb'"
            :style="stylePreset"
            :background="bgPreset"
            @loaded="onLoaded"
          />
      </div>
      <div class="viewer-sidebar">
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
          <span class="label">模型朝向</span>
          <div class="style-grid" style="grid-template-columns: repeat(2, 1fr); margin-top: 8px">
            <el-button size="small" :disabled="!info.sizeLabel" @click="viewer?.rotateOrientation('x', 90)">俯仰 90°</el-button>
            <el-button size="small" :disabled="!info.sizeLabel" @click="viewer?.rotateOrientation('z', 90)">侧转 90°</el-button>
            <el-button size="small" :disabled="!info.sizeLabel" @click="viewer?.rotateOrientation('z', 180)">翻转 180°</el-button>
            <el-button size="small" :disabled="!info.sizeLabel" @click="viewer?.restoreOrientation()">恢复朝向</el-button>
          </div>
          <p class="ver-desc">{{ orientationHint }}仅影响预览。</p>
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
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowLeft, Crop, Download, Refresh } from '@element-plus/icons-vue'
import ModelViewer, { type StylePreset, type BackgroundPreset } from '../../three/ModelViewer.vue'
import { modelApi, fmtSize, fmtTime, errMsg, isPhotoTexture, type ModelDetail } from '../../api'

const props = defineProps<{ id: string }>()
const viewer = ref<InstanceType<typeof ModelViewer> | null>(null)
const model = ref<ModelDetail | null>(null)
const fileData = ref<ArrayBuffer | null>(null)
const pending = ref(true)
const fetchError = ref('')
const stylePreset = ref<StylePreset>('original')
const bgPreset = ref<BackgroundPreset>('gradient')
const counts = ref({ vertices: 0, faces: 0, points: 0 })
const info = ref({ sizeLabel: '' })
const selectMode = ref(false)
const orientationHint = computed(() => {
  if (model.value?.stage_origin === 'PHOTO_TEXTURE_UPRIGHT') return '导出模型已校正默认朝向。'
  if (model.value?.display_up) return '默认朝向根据拍摄视角估算。'
  return '未检测到可靠的上方向，可手动校正。'
})

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

async function onRegionSelected(payload: { bbox_min: number[]; bbox_max: number[]; camera: Record<string, number[]> }) {
  try {
    const region = await modelApi.saveRegion(Number(props.id), {
      bbox_min: payload.bbox_min,
      bbox_max: payload.bbox_max,
      camera: payload.camera,
      note: '查看器框选',
    })
    const job = await modelApi.createLocalUpdate(Number(props.id), region.id)
    ElMessage.success(`选区 #${region.id} 已保存，区域提取任务 #${job.id} 已创建（阶段 8 原型：区域提取，非局部重建）`)
    selectMode.value = false
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
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
    fetchError.value = `模型加载失败：${errMsg(e)}`
  } finally {
    pending.value = false
  }
})
</script>

<style scoped>
.toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; margin-bottom: 14px; }
.toolbar h3 { margin: 0; }
.toolbar .el-button { margin: 0; }
.spacer { flex: 1; }
.viewer-layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(235px, 280px); gap: 16px; }
.viewer-wrap { height: max(320px, calc(100dvh - 160px)); }
.panel { max-height: max(320px, calc(100dvh - 160px)); overflow: auto; }
@media (max-width: 760px) {
  .viewer-layout { grid-template-columns: minmax(0, 1fr); }
  .viewer-wrap { height: clamp(240px, 58dvh, 480px); }
  .panel { max-height: none; }
}
.style-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
.style-grid .el-button { margin: 0; width: 100%; }
.label { font-size: 13px; color: #606266; font-weight: 600; }
.ver-desc { font-size: 11px; color: #909399; }
</style>
