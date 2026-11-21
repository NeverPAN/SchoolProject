<template>
  <div>
    <div class="toolbar">
      <el-button :icon="ArrowLeft" text @click="$router.back()">返回</el-button>
      <h3>三维模型查看 #{{ id }}</h3>
      <el-tag v-if="model?.experiment_name || model?.job_id" size="small">
        {{ model.experiment_name || `任务 #${model.job_id}` }}<template v-if="model.experiment_name && model.job_id"> · 任务 #{{ model.job_id }}</template>
      </el-tag>
      <el-tag v-if="model?.pipeline_mode === 'experiment'" size="small" type="warning">实验归档</el-tag>
      <el-tag v-else-if="model?.pipeline_mode === 'mock'" size="small" type="info">模拟</el-tag>
      <el-tag v-if="model?.stage_origin === 'LOCAL_REGION'" type="info" size="small">区域提取结果</el-tag>
      <el-tag v-else-if="model?.stage_origin === 'LOCAL_POINT_FUSION'" type="info" size="small">局部重建 · 融合点云</el-tag>
      <el-tag v-else-if="model?.stage_origin === 'LOCAL_MESH_PATCH'" type="info" size="small">局部网格补丁</el-tag>
      <el-tag v-else-if="model && (isPhotoTexture(model) || photoColorAvailable(model))" type="success" size="small">照片贴图 · GLB</el-tag>
      <div class="spacer" />
      <el-button
        :icon="Crop" :type="selectionMode === 'extraction' ? 'primary' : 'default'" :aria-pressed="selectionMode === 'extraction'"
        :disabled="!canSelect || submitting || isReconstruction" @click="selectionMode === 'extraction' ? exitSelection() : enterSelection('extraction')"
      >{{ selectionMode === 'extraction' ? '取消框选' : '框选区域' }}</el-button>
      <el-button
        v-if="model?.stage_origin === 'LOCAL_POINT_FUSION'"
        :disabled="submitting || selectMode" @click="submitMeshPatch"
      >生成局部网格补丁</el-button>
      <el-button
        v-if="model?.local_reconstruction_available === true"
        :type="isReconstruction ? 'primary' : 'default'" :aria-pressed="isReconstruction"
        :disabled="!canSelect || submitting || selectionMode === 'extraction'"
        @click="isReconstruction ? exitSelection() : enterSelection('reconstruction')"
      >{{ isReconstruction ? '取消局部重建' : '局部重建' }}</el-button>
      <el-button :icon="Refresh" :disabled="!info.sizeLabel || selectMode" @click="viewer?.resetView()">重置视角</el-button>
      <el-button type="primary" :icon="Download" @click="onDownload">下载模型</el-button>
    </div>

    <div v-if="model?.local_extraction" class="region-panel">
      <strong>区域提取（非局部重建） · 选区 #{{ model.local_extraction.region_id }}</strong>
      <router-link :to="{ name: 'model-viewer', params: { id: model.local_extraction.parent_model_id }, query: { extraction: model.id } }">
        回父模型 #{{ model.local_extraction.parent_model_id }} 看选区
      </router-link>
    </div>
    <div v-if="reviewRegion && !selectMode" class="region-panel" role="status">
      <strong>区域提取 #{{ reviewRegion.resultId }} 的原选区 #{{ reviewRegion.regionId }}</strong>
      <p>橙色框标出父模型上的近似包围盒，不是精确裁剪边界。</p>
      <el-button text size="small" @click="dismissReviewRegion">隐藏选区</el-button>
    </div>

    <div v-if="selectMode || regionJobId" class="region-panel">
      <div v-if="selectMode">
        <template v-if="isReconstruction">
          <strong>局部重建（输出融合点云）</strong>
          <p v-if="isSeedMode">在画布上框选已验证范围内的区域，或直接使用已验证选区。此实验仅支持指定的三张项目照片。结果为独立的融合点云，不会修改原模型、替换网格或修补接缝。</p>
          <p v-else>选择 1–32 张本项目新增照片，系统将其注册到父模型并做局部融合。结果为独立的点云候选，不会修改原模型或生成网格；照片需与父场景有足够重叠。</p>
          <el-button v-if="validatedRegion" :disabled="submitting" size="small" @click="useValidatedRegion">使用已验证选区</el-button>
          <div v-if="photosPending" role="status">正在加载项目照片…</div>
          <div v-else-if="photosError" class="region-feedback">
            <span role="alert">{{ photosError }}</span>
            <el-button text @click="loadProjectPhotos">重试加载照片</el-button>
          </div>
          <p v-else-if="!hasRequiredPhotos" role="status">
            {{ isSeedMode ? `项目缺少实验照片。请先到项目中上传原片 ${requiredPhotoNames.join('、')}。` : '项目还没有照片，请先到项目上传要注册的新增照片。' }}
          </p>
          <el-select
            v-else v-model="selectedPhotoIds" multiple filterable
            :multiple-limit="isSeedMode ? requiredPhotoNames.length : 32" :disabled="submitting" class="photo-picker"
            aria-label="用于局部重建的项目照片" :placeholder="isSeedMode ? '选择三张已验证的实验照片' : '选择要注册的新增照片（1–32 张）'"
          >
            <el-option
              v-for="photo in projectPhotos" :key="photo.id" :value="photo.id"
              :label="`${photo.original_filename} · #${photo.id}`"
              :disabled="isSeedMode
                ? !requiredPhotoNames.includes(photo.original_filename)
                : (model?.local_reconstruction_image_ids === undefined
                    && !selectedPhotoIds.includes(photo.id)
                    && selectedPhotoNames.includes(photo.original_filename))"
            />
          </el-select>
        </template>
        <template v-else>
          <strong>区域提取（非局部重建）</strong>
          <p>在画布上拖拽框选可见网格，橙色框为近似包围盒。提取将导出独立模型，不修改原模型，也不会重建或融合。</p>
          <el-button v-if="latestSavedRegion" :disabled="submitting" size="small" @click="useSavedRegion">
            使用已保存选区 #{{ latestSavedRegion.id }}
          </el-button>
        </template>
        <div class="region-feedback">
          <span v-if="selectionError" role="alert">{{ selectionError }}</span>
          <span v-else role="status">{{ selection ? '已框选区域。' : '等待框选' }}</span>
        </div>
        <el-button type="primary" :disabled="!canSubmitRegion" :loading="submitting" @click="submitRegion">
          {{ savedRegionId ? `重试创建${activeJobLabel}任务` : `创建${activeJobLabel}任务` }}
        </el-button>
      </div>
      <div v-else>
        <span role="status">{{ createdJobLabel }}任务 #{{ regionJobId }} 已创建。</span>
        <router-link :to="{ name: 'job-detail', params: { id: regionJobId } }">查看{{ createdJobLabel }}任务</router-link>
      </div>
    </div>

    <div class="viewer-layout">
      <div class="viewer-wrap">
          <ModelViewer
            ref="viewer"
            :select-mode="selectMode"
            :selection-locked="submitting"
            @region-selected="setRegionSelection"
            @region-cleared="clearSelection"
            @region-failed="(reason) => selectionError = reason"
            :file-data="fileData"
            :pending="pending"
            :fetch-error="fetchError"
            :display-up="model?.display_up"
            :legacy-srgb-vertex-colors="model?.legacy_srgb_vertex_colors"
            :file-format="model?.format || 'glb'"
            :style="stylePreset"
            :photo-exposure="photoExposure"
            :background="bgPreset"
            @loaded="onLoaded"
          />
      </div>
      <div class="viewer-sidebar">
        <el-card shadow="never" class="panel">
          <template #header><span>展示风格</span></template>
          <div class="style-grid">
            <el-button
              v-for="s in availableStyles"
              :key="s.key"
              :type="stylePreset === s.key ? 'primary' : 'default'"
              :disabled="selectMode && s.key === 'pointcloud'"
              size="small"
              @click="stylePreset = s.key"
            >{{ s.label }}</el-button>
          </div>
          <div v-if="stylePreset === 'photo-color'">
            <p class="ver-desc">照片纹理不受灯光影响。曝光 0 保留图集原色；仅影响预览和对照。</p>
            <label class="label" for="photo-exposure">预览曝光（EV）</label>
            <div><input id="photo-exposure" v-model.number="photoExposure" type="range" min="-1" max="1" step="0.1" /> {{ photoExposure.toFixed(1) }}</div>
            <el-button size="small" text @click="photoExposure = 0">恢复原色</el-button>
          </div>
          <el-divider />
          <span class="label">模型朝向</span>
          <div class="style-grid" style="grid-template-columns: repeat(2, 1fr); margin-top: 8px">
            <el-button size="small" :disabled="!info.sizeLabel || selectMode" @click="viewer?.rotateOrientation('x', 90)">俯仰 90°</el-button>
            <el-button size="small" :disabled="!info.sizeLabel || selectMode" @click="viewer?.rotateOrientation('z', 90)">侧转 90°</el-button>
            <el-button size="small" :disabled="!info.sizeLabel || selectMode" @click="viewer?.rotateOrientation('z', 180)">翻转 180°</el-button>
            <el-button size="small" :disabled="!info.sizeLabel || selectMode" @click="viewer?.restoreOrientation()">恢复朝向</el-button>
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
              <el-descriptions-item v-if="model.experiment" label="实验版本">{{ model.experiment.code }}</el-descriptions-item>
              <el-descriptions-item v-if="model.experiment" label="来源版本">{{ model.experiment.parent || '无（基线）' }}</el-descriptions-item>
              <el-descriptions-item v-if="model.experiment" label="实验范围">{{ model.experiment.scope }}</el-descriptions-item>
              <el-descriptions-item label="类型">{{ modelTypeLabel(model.model_type) }}</el-descriptions-item>
              <el-descriptions-item label="格式">{{ model.format.toUpperCase() }}</el-descriptions-item>
              <el-descriptions-item label="文件大小">{{ fmtSize(model.file_size) }}</el-descriptions-item>
              <el-descriptions-item v-for="item in geometryRows" :key="item.label" :label="item.label">
                {{ item.value.toLocaleString() }}
              </el-descriptions-item>
              <el-descriptions-item v-if="model.texture_assets && model.texture_assets.materials_total > 0" label="贴图资产（实测文件）">
                材质 {{ model.texture_assets.materials_with_base_color_texture }}/{{ model.texture_assets.materials_total }} 带纹理 ·
                UV 图元 {{ model.texture_assets.primitives_with_uv }}/{{ model.texture_assets.primitives_with_uv ? model.texture_assets.primitives_total : model.texture_assets.primitives_total }}
              </el-descriptions-item>
              <el-descriptions-item label="预览包围盒（模型单位）">{{ info.sizeLabel || '待加载' }}</el-descriptions-item>
              <el-descriptions-item :label="isArchive ? '归档时间（UTC）' : '模型登记时间（UTC）'">
                {{ recordTime(model.created_at) }}
              </el-descriptions-item>
            </el-descriptions>
            <el-divider />
            <span class="label">{{ isArchive ? '文件登记记录' : '模型版本' }}</span>
            <p v-if="isArchive" class="ver-desc">登记编号只针对当前文件，不表示实验迭代顺序；请以上方实验版本和来源版本为准。</p>
            <el-timeline style="margin-top: 12px">
              <el-timeline-item
                v-for="v in model.versions || []"
                :key="v.id"
                :timestamp="recordTime(v.created_at)"
                type="primary"
              >
                {{ isArchive ? `登记 #${v.version_number}` : `v${v.version_number} · ${versionLabel(v.change_type)}` }}
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
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft, Crop, Download, Refresh } from '@element-plus/icons-vue'
import ModelViewer, { type StylePreset, type BackgroundPreset, type RegionSelection } from '../../three/ModelViewer.vue'
import { imageApi, modelApi, fmtSize, errMsg, isPhotoTexture, photoColorAvailable, type ImageBrief, type ModelDetail } from '../../api'

const props = defineProps<{ id: string }>()
const route = useRoute()
const viewer = ref<InstanceType<typeof ModelViewer> | null>(null)
const model = ref<ModelDetail | null>(null)
const fileData = ref<ArrayBuffer | null>(null)
const pending = ref(true)
const fetchError = ref('')
const stylePreset = ref<StylePreset>('original')
const photoExposure = ref(0)
const bgPreset = ref<BackgroundPreset>('gradient')
const counts = ref({ vertices: 0, faces: 0, points: 0 })
const info = ref({ sizeLabel: '' })
const isArchive = computed(() => model.value?.pipeline_mode === 'experiment' || !!model.value?.experiment)
const geometryRows = computed(() => {
  const m = model.value
  if (!m) return []
  if (m.model_type.includes('pointcloud')) return [{ label: '点云采样点数', value: m.point_count }]
  return [{ label: '网格顶点数', value: m.vertex_count }, { label: '三角面数', value: m.face_count }]
})

function recordTime(value: string | null) {
  // The API currently serializes database UTC timestamps without an offset.
  if (!value) return '未知（未留存）'
  const timestamp = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`
  return new Date(timestamp).toLocaleString('zh-CN', { hour12: false, timeZone: 'UTC' })
}

type SelectionMode = 'extraction' | 'reconstruction'
const requiredPhotoNames = ['DSC_1290.JPG', 'DSC_1291.JPG', 'DSC_1292.JPG']
const selectionMode = ref<SelectionMode | null>(null)
const selectMode = computed(() => selectionMode.value !== null)
const isReconstruction = computed(() => selectionMode.value === 'reconstruction')
const selection = ref<RegionSelection | null>(null)
const selectionError = ref('')
const submitting = ref(false)
const savedRegionId = ref<number | null>(null)
const regionJobId = ref<number | null>(null)
const createdJobLabel = ref('')
const activeJobLabel = computed(() => isReconstruction.value ? '局部重建' : '区域提取')

async function submitMeshPatch() {
  if (!model.value || submitting.value || model.value.stage_origin !== 'LOCAL_POINT_FUSION') return
  submitting.value = true
  selectionError.value = ''
  try {
    const job = await modelApi.createLocalMesh(model.value.id)
    regionJobId.value = job.id
    createdJobLabel.value = '局部网格补丁'
  } catch (e) {
    selectionError.value = `创建局部网格补丁任务失败：${errMsg(e)}`
  } finally {
    submitting.value = false
  }
}
const projectPhotos = ref<ImageBrief[]>([])
const selectedPhotoIds = ref<number[]>([])
const photosPending = ref(false)
const photosError = ref('')
let photoRequest: AbortController | undefined

const isSeedMode = computed(() => model.value?.local_reconstruction_mode === 'seed'
  || (model.value?.local_reconstruction_mode === undefined && model.value?.local_reconstruction_image_ids !== undefined))
const maxSelectablePhotos = computed(() => isSeedMode.value ? requiredPhotoNames.length : 32)
const hasRequiredPhotos = computed(() => {
  if (model.value?.local_reconstruction_mode === 'general') return projectPhotos.value.length >= 1
  return model.value?.local_reconstruction_image_ids === undefined
    ? requiredPhotoNames.every((name) => projectPhotos.value.some((photo) => photo.original_filename === name))
    : projectPhotos.value.length >= requiredPhotoNames.length
})
const selectedPhotos = computed(() => projectPhotos.value.filter((photo) => selectedPhotoIds.value.includes(photo.id)))
const selectedPhotoNames = computed(() => selectedPhotos.value.map((photo) => photo.original_filename))
const canSelect = computed(() => !pending.value && !fetchError.value && counts.value.faces > 0 && stylePreset.value !== 'pointcloud')
const canSubmitRegion = computed(() => canSelect.value && selectMode.value && !!selection.value && !submitting.value && (
  !isReconstruction.value || (model.value?.local_reconstruction_available === true && !photosPending.value && !photosError.value && (
    (model.value.local_reconstruction_mode === 'general' && selectedPhotoIds.value.length >= 1
      && selectedPhotoIds.value.length <= maxSelectablePhotos.value
      && selectedPhotos.value.length === selectedPhotoIds.value.length)
    || (selectedPhotoIds.value.length === requiredPhotoNames.length && selectedPhotos.value.length === requiredPhotoNames.length && (
      model.value.local_reconstruction_image_ids !== undefined || requiredPhotoNames.every((name) => selectedPhotoNames.value.includes(name))
    ))
  ))
))

function isPoint(value: unknown): value is [number, number, number] {
  return Array.isArray(value) && value.length === 3 && value.every(Number.isFinite)
}

const reviewRegion = ref<{ resultId: number; regionId: number; selection: RegionSelection } | null>(null)
let reviewRequest = 0

function dismissReviewRegion() {
  reviewRequest++
  if (reviewRegion.value && !selectMode.value) viewer.value?.clearRegionHelper()
  reviewRegion.value = null
}

watch(() => [route.query.extraction, model.value] as const, async ([from, parent]) => {
  dismissReviewRegion()
  if (!parent || typeof from !== 'string' || !/^\d+$/.test(from)) return
  const resultId = Number(from)
  if (!Number.isSafeInteger(resultId) || resultId <= 0 || resultId === parent.id) return
  const request = reviewRequest
  try {
    const result = await modelApi.get(resultId)
    const source = result.local_extraction
    if (request !== reviewRequest || selectMode.value || !source || result.id !== resultId ||
        result.stage_origin !== 'LOCAL_REGION' || result.project_id !== parent.project_id || source.parent_model_id !== parent.id) return
    const { min, max } = source.bounds
    if (!isPoint(min) || !isPoint(max) || min.some((v, i) => v >= max[i])) return
    reviewRegion.value = { resultId, regionId: source.region_id, selection: { bbox_min: min, bbox_max: max } }
    if (info.value.sizeLabel) viewer.value?.showRegion(reviewRegion.value.selection)
  } catch {
    // The parent remains usable if the result was deleted or is unavailable.
  }
})

const validatedRegion = computed<RegionSelection | null>(() => {
  const bounds = model.value?.local_reconstruction_bounds
  if (!bounds || !isPoint(bounds.min) || !isPoint(bounds.max) || bounds.min.some((v, i) => v >= bounds.max[i])) return null
  return { bbox_min: bounds.min, bbox_max: bounds.max }
})
const orientationHint = computed(() => {
  if (model.value?.stage_origin === 'PHOTO_TEXTURE_UPRIGHT') return '导出模型已校正默认朝向。'
  if (model.value?.display_up) return '默认朝向根据拍摄视角估算。'
  return '未检测到可靠的上方向，可手动校正。'
})

const styles: { key: StylePreset; label: string }[] = [
  { key: 'original', label: '写实' },
  { key: 'photo-color', label: '原色贴图' },
  { key: 'morandi', label: '莫兰迪' },
  { key: 'monochrome', label: '单色' },
  { key: 'wireframe', label: '线框' },
  { key: 'pointcloud', label: '点云' },
]

const availableStyles = computed(() => styles.filter((s) => s.key !== 'photo-color' || (model.value && photoColorAvailable(model.value))))

function modelTypeLabel(t: string) {
  return { sparse_pointcloud: '稀疏点云', dense_pointcloud: '稠密点云', mesh: '网格', web: 'Web 展示' }[t] || t
}
function versionLabel(t: string) {
  return { initial: '首次生成', append_images: '追加照片更新', manual: '手动版本',
    rerun: '同输入重跑', inputs_changed: '输入已变更', inputs_unknown: '历史输入无法核对' }[t] || t
}

function onLoaded(i: { vertices: number; faces: number; points: number; sizeLabel: string }) {
  counts.value = { vertices: i.vertices, faces: i.faces, points: i.points }
  info.value = { sizeLabel: i.sizeLabel }
  if (reviewRegion.value && !selectMode.value) viewer.value?.showRegion(reviewRegion.value.selection)
}

function clearSelection() {
  selection.value = null
  selectionError.value = ''
  savedRegionId.value = null
}

function setRegionSelection(region: RegionSelection) {
  clearSelection()
  const envelope = validatedRegion.value
  if (isReconstruction.value && envelope && region.bbox_min.some((min, i) => (
    min < envelope.bbox_min[i] || region.bbox_max[i] > envelope.bbox_max[i]
  ))) {
    selectionError.value = '框选超出已验证范围，请重新框选或使用已验证选区。'
    return
  }
  selection.value = region
}

function useValidatedRegion() {
  if (!isReconstruction.value || !validatedRegion.value || submitting.value) return
  setRegionSelection(validatedRegion.value)
  viewer.value?.showRegion(validatedRegion.value)
}

const savedRegions = ref<{ id: number; bbox_min: number[]; bbox_max: number[] }[]>([])
const latestSavedRegion = computed(() => savedRegions.value.length ? savedRegions.value[0] : null)

async function loadSavedRegions() {
  if (!model.value || submitting.value) return
  try {
    const regions = await modelApi.listRegions(model.value.id)
    savedRegions.value = [...regions].sort((a, b) => b.id - a.id)
  } catch {
    savedRegions.value = []  // 选区列表不可用时仍可手动框选
  }
}

function useSavedRegion() {
  const latest = latestSavedRegion.value
  if (!latest || submitting.value) return
  const region = { bbox_min: latest.bbox_min, bbox_max: latest.bbox_max } as RegionSelection
  setRegionSelection(region)
  viewer.value?.showRegion(region)
}

async function loadProjectPhotos() {
  if (!isReconstruction.value || !model.value || photosPending.value) return
  const request = new AbortController()
  photoRequest = request
  const allowed = model.value.local_reconstruction_image_ids
  photosPending.value = true
  photosError.value = ''
  try {
    const images = await imageApi.list(model.value.project_id, request.signal)
    if (photoRequest !== request) return
    projectPhotos.value = model.value.local_reconstruction_mode === 'general'
      ? images
      : allowed === undefined
        ? images.filter((image) => requiredPhotoNames.includes(image.original_filename))
        : images.filter((image) => allowed.includes(image.id))
  } catch (e) {
    if (photoRequest === request) photosError.value = `加载项目照片失败：${errMsg(e)}`
  } finally {
    if (photoRequest === request) {
      photoRequest = undefined
      photosPending.value = false
    }
  }
}

function resetPhotos() {
  photoRequest?.abort()
  photoRequest = undefined
  projectPhotos.value = []
  selectedPhotoIds.value = []
  photosPending.value = false
  photosError.value = ''
}

function enterSelection(mode: SelectionMode) {
  if (!canSelect.value || selectMode.value || submitting.value ||
      (mode === 'reconstruction' && model.value?.local_reconstruction_available !== true)) return
  dismissReviewRegion()
  clearSelection()
  regionJobId.value = null
  selectionMode.value = mode
  if (mode === 'reconstruction') void loadProjectPhotos()
  else void loadSavedRegions()
}

function exitSelection() {
  if (submitting.value) return
  selectionMode.value = null
  clearSelection()
  resetPhotos()
}

function onEscape(event: KeyboardEvent) {
  if (event.key === 'Escape' && !event.defaultPrevented && !event.cancelBubble && selectMode.value) exitSelection()
}

async function submitRegion() {
  if (!canSubmitRegion.value || !selection.value || !model.value) return
  const regionSelection = selection.value
  const modelId = model.value.id
  const reconstruct = isReconstruction.value
  const jobLabel = activeJobLabel.value
  const imageIds = [...selectedPhotoIds.value]
  submitting.value = true
  selectionError.value = ''
  try {
    if (savedRegionId.value === null) {
      const region = await modelApi.saveRegion(modelId, { ...regionSelection, note: reconstruct ? '局部重建选区' : '查看器框选' })
      savedRegionId.value = region.id
    }
    const job = reconstruct
      ? await modelApi.createLocalReconstruction(modelId, savedRegionId.value, imageIds)
      : await modelApi.createRegionExtraction(modelId, savedRegionId.value)
    regionJobId.value = job.id
    createdJobLabel.value = jobLabel
    selectionMode.value = null
    clearSelection()
    resetPhotos()
  } catch (e) {
    const prefix = savedRegionId.value ? `选区已保存，创建${jobLabel}任务失败` : '保存选区失败'
    selectionError.value = `${prefix}：${errMsg(e)}`
  } finally {
    submitting.value = false
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

onBeforeUnmount(() => {
  reviewRequest++
  window.removeEventListener('keydown', onEscape)
  photoRequest?.abort()
  photoRequest = undefined
})
onMounted(async () => {
  window.addEventListener('keydown', onEscape)
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
.region-panel { margin-bottom: 12px; padding: 10px 14px; background: #fff7ec; border-radius: 8px; }
.region-panel p, .region-feedback { margin: 6px 0; font-size: 13px; }
.region-panel [role='alert'] { color: #a84127; }
.region-panel a { margin-inline-start: 12px; color: var(--el-color-primary-dark-2); }
.photo-picker { display: flex; width: min(100%, 560px); margin-top: 8px; }
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
