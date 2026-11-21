<template>
  <div v-if="project">
    <div class="toolbar">
      <el-button :icon="ArrowLeft" text @click="$router.push({ name: 'projects' })">返回</el-button>
      <h3>{{ project.name }}</h3>
      <el-tag size="small" type="info">{{ project.description || '无描述' }}</el-tag>
      <div class="spacer" />
      <el-button :icon="Search" :loading="qcLoading" :disabled="uploading" @click="onQualityCheck">质量检查</el-button>
      <el-button type="primary" :icon="VideoPlay" :disabled="!canSubmitJob" @click="jobDialogVisible = true">
        创建重建任务
      </el-button>
    </div>

    <el-row :gutter="16" class="stats">
      <el-col :span="6"><el-card shadow="never"><div class="stat"><div class="num">{{ project.image_count }}</div><div class="label">图片数量</div></div></el-card></el-col>
      <el-col :span="6"><el-card shadow="never"><div class="stat"><div class="num">{{ fmtSize(project.total_image_bytes) }}</div><div class="label">图片总大小</div></div></el-card></el-col>
      <el-col :span="6"><el-card shadow="never"><div class="stat"><div class="num">{{ qualityText }}</div><div class="label">质量检查</div></div></el-card></el-col>
      <el-col :span="6"><el-card shadow="never"><div class="stat"><div class="num">{{ project.model_count }}</div><div class="label">模型数量</div></div></el-card></el-col>
    </el-row>

    <!-- 上传区 -->
    <el-card shadow="never" class="section">
      <template #header>
        <div class="card-head">
          <span>图片管理</span>
          <input ref="filePicker" type="file" hidden multiple accept=".jpg,.jpeg,.png,.tif,.tiff,.bmp,.webp" @change="onFilePick" />
          <el-button type="primary" plain :icon="Upload" :disabled="uploading" @click="filePicker?.click()">上传图片</el-button>
        </div>
      </template>
      <div v-if="uploadSummary" class="upload-summary" data-testid="upload-summary">
        <el-progress :percentage="uploadPct" aria-label="图片上传总进度" />
        <span role="status">{{ uploadSummary }}</span>
      </div>
      <ul v-if="uploadFailures.length" class="upload-failures" aria-label="上传失败的图片">
        <li v-for="item in uploadFailures" :key="item.id">
          <span><strong>{{ item.file.name }}</strong>：{{ item.reason }}</span>
          <el-button size="small" :disabled="uploading" :aria-label="`重试 ${item.file.name}`" @click="uploadFiles([item])">重试</el-button>
          <el-button size="small" text :disabled="uploading" :aria-label="`移除 ${item.file.name}`" @click="removeUploadFailure(item.id)">移除</el-button>
        </li>
      </ul>
      <div v-if="project.images.length === 0" class="empty">暂无图片，请先上传多视角图片（≥3 张，正式重建建议 100+ 张）</div>
      <div class="thumb-grid">
        <div v-for="img in project.images" :key="img.id" class="thumb-item">
          <div class="thumb-box">
            <img v-if="thumbs[img.id]" :src="thumbs[img.id]" loading="lazy" />
            <div v-else class="thumb-loading"><el-icon class="is-loading"><Loading /></el-icon></div>
            <el-tag v-if="img.quality_status === 'ok'" class="qtag" type="success" size="small">OK</el-tag>
            <el-tag v-else-if="img.quality_status === 'warning'" class="qtag" type="warning" size="small">警告</el-tag>
            <el-tag v-else-if="img.quality_status === 'invalid'" class="qtag" type="danger" size="small">无效</el-tag>
            <el-tag v-else class="qtag" type="info" size="small">待检</el-tag>
          </div>
          <div class="thumb-name" :title="img.original_filename">{{ img.original_filename }}</div>
          <div class="thumb-meta">{{ img.width }}×{{ img.height }} · {{ fmtSize(img.file_size) }}</div>
          <el-button size="small" type="danger" text @click="onDeleteImage(img)">删除</el-button>
        </div>
      </div>
    </el-card>

    <!-- 任务列表 -->
    <el-card shadow="never" class="section">
      <template #header><span>任务列表</span></template>
      <el-table :data="jobs" size="small" @row-click="(row: Job) => $router.push({ name: 'job-detail', params: { id: row.id } })">
        <el-table-column label="任务 ID" width="80"><template #default="{ row }">#{{ row.id }}</template></el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ STATUS_LABELS[row.status] || row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="当前阶段" width="140">
          <template #default="{ row }">{{ row.current_stage ? STAGE_LABELS[row.current_stage] || row.current_stage : '-' }}</template>
        </el-table-column>
        <el-table-column label="进度" width="180">
          <template #default="{ row }"><el-progress :percentage="Math.round(row.progress)" :status="row.status === 'FAILED' ? 'exception' : row.status === 'SUCCEEDED' ? 'success' : undefined" /></template>
        </el-table-column>
        <el-table-column label="模式" min-width="180"><template #default="{ row }">{{ pipelineModeLabels[row.pipeline_mode] || 'COLMAP' }}</template></el-table-column>
        <el-table-column label="创建时间" width="170"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click.stop="$router.push({ name: 'job-detail', params: { id: row.id } })">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 模型列表 -->
    <el-card shadow="never" class="section">
      <template #header><span>模型列表</span></template>
      <el-table :data="models" size="small">
        <el-table-column label="模型 ID" width="80"><template #default="{ row }">#{{ row.id }}</template></el-table-column>
        <el-table-column label="类型" min-width="185">
          <template #default="{ row }">
            {{ modelTypeLabel(row.model_type) }} ({{ row.format.toUpperCase() }})
            <el-tag v-if="row.stage_origin === 'LOCAL_REGION'" type="info" size="small">区域提取结果</el-tag>
            <el-tag v-else-if="row.stage_origin === 'LOCAL_POINT_FUSION'" type="info" size="small">局部重建 · 融合点云</el-tag>
            <el-tag v-else-if="isPhotoTexture(row)" type="success" size="small">照片贴图</el-tag>
            <el-tag v-else-if="row.stage_origin === 'WEB_REEXPORT'" type="info" size="small">修正版</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="版本名称" min-width="210" show-overflow-tooltip>
          <template #default="{ row }">
            {{ jobsById.get(row.job_id)?.experiment_name || (row.job_id ? `任务 #${row.job_id}` : '未命名') }}
            <el-tag v-if="jobsById.get(row.job_id)?.pipeline_mode === 'experiment'" size="small" type="warning">实验归档</el-tag>
            <el-tag v-else-if="jobsById.get(row.job_id)?.pipeline_mode === 'mock'" size="small" type="info">模拟</el-tag>
            <small v-if="jobsById.get(row.job_id)?.experiment_name"> · #{{ row.job_id }}</small>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="110"><template #default="{ row }">{{ fmtSize(row.file_size) }}</template></el-table-column>
        <el-table-column label="顶点/点数" width="130"><template #default="{ row }">{{ (row.vertex_count || row.point_count || 0).toLocaleString() }}</template></el-table-column>
        <el-table-column label="面数" width="120"><template #default="{ row }">{{ (row.face_count || 0).toLocaleString() }}</template></el-table-column>
        <el-table-column label="生成时间" width="170"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
        <el-table-column label="操作" width="180">
          <template #default="{ row }">
            <el-button v-if="row.model_type === 'web' || row.format === 'glb' || row.format === 'ply'" size="small" type="primary" plain @click="$router.push({ name: 'model-viewer', params: { id: row.id } })">查看</el-button>
            <el-button size="small" @click="modelApi.download(row.id)">下载</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 重建任务配置 -->
    <el-dialog v-model="jobDialogVisible" title="创建重建任务" width="480px">
      <el-form label-width="110px">
        <el-form-item label="版本名称">
          <el-input v-model="jobForm.experiment_name" maxlength="120" placeholder="可选，如 Bridge · 双卡 3000" />
        </el-form-item>
        <el-form-item label="质量预设">
          <el-radio-group v-model="jobForm.preset">
            <el-radio-button value="fast">快速</el-radio-button>
            <el-radio-button value="standard">标准</el-radio-button>
            <el-radio-button value="high">高精度</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="匹配策略">
          <el-select v-model="jobForm.matcher" style="width: 100%">
            <el-option label="自动选择" value="auto" />
            <el-option label="穷举匹配（无序照片）" value="exhaustive" />
            <el-option label="顺序匹配（视频帧/连续拍摄）" value="sequential" />
          </el-select>
        </el-form-item>
        <el-form-item label="稠密重建">
          <el-switch v-model="jobForm.enable_dense" />
          <span class="hint">生成稠密点云（GPU 推荐，耗时较长）</span>
        </el-form-item>
        <el-form-item label="网格生成">
          <el-switch v-model="jobForm.enable_mesh" />
          <span class="hint">基于稠密点云生成表面网格与 GLB</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="jobDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" data-testid="submit-job-btn" @click="onSubmitJob">提交任务</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, Loading, Search, Upload, VideoPlay } from '@element-plus/icons-vue'
import {
  imageApi, jobApi, modelApi, projectApi,
  fmtSize, fmtTime, errMsg,
  STAGE_LABELS, STATUS_LABELS, isPhotoTexture,
  type ImageBrief, type Job, type ModelItem, type ProjectDetail,
} from '../../api'

const props = defineProps<{ id: string }>()
const project = ref<ProjectDetail | null>(null)
const jobs = ref<Job[]>([])
const pipelineModeLabels: Record<string, string> = {
  local_update: '区域提取（非局部重建）',
  local_reconstruct: '局部重建（融合点云）',
  mock: '模拟',
  experiment: '实验归档',
}
const models = ref<ModelItem[]>([])
const thumbs = ref<Record<number, string>>({})
const qcLoading = ref(false)
const uploading = ref(false)
const uploadPct = ref(0)
const uploadSummary = ref('')
const filePicker = ref<HTMLInputElement | null>(null)
interface UploadEntry { id: number; file: File }
const uploadFailures = ref<(UploadEntry & { reason: string })[]>([])
const MAX_BATCH_FILES = 12
const MAX_BATCH_BYTES = 64 * 1024 * 1024
let uploadRequest: AbortController | undefined
let nextUploadId = 0
const jobDialogVisible = ref(false)
const submitting = ref(false)
const jobsById = computed(() => new Map(jobs.value.map((job) => [job.id, job])))
const jobForm = ref({ experiment_name: '', preset: 'standard', matcher: 'auto', enable_dense: true, enable_mesh: true })

const canSubmitJob = computed(() => !uploading.value && (project.value?.image_count ?? 0) >= 3)
const qualityText = computed(() => {
  const q = project.value?.quality_summary || {}
  return `OK ${q.ok || 0} / 警告 ${q.warning || 0} / 无效 ${q.invalid || 0}`
})

function statusType(s: string) {
  if (s === 'SUCCEEDED') return 'success'
  if (s === 'FAILED') return 'danger'
  if (s === 'CANCELLED' || s === 'CANCEL_REQUESTED') return 'info'
  return 'warning'
}
function modelTypeLabel(t: string) {
  return { sparse_pointcloud: '稀疏点云', dense_pointcloud: '稠密点云', mesh: '网格', web: 'Web 展示' }[t] || t
}

async function loadAll() {
  try {
    project.value = await projectApi.get(Number(props.id))
    const [j, m] = await Promise.all([jobApi.list(Number(props.id)), modelApi.list(Number(props.id))])
    jobs.value = j
    models.value = m
    // 加载缩略图
    for (const img of project.value.images) {
      if (img.has_thumbnail && !thumbs.value[img.id]) {
        imageApi.thumbnailUrl(img.id).then((url) => (thumbs.value[img.id] = url)).catch(() => {})
      }
    }
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

function* uploadBatches(entries: UploadEntry[]) {
  let batch: UploadEntry[] = []
  let bytes = 0
  const names = new Set<string>()
  for (const entry of entries) {
    // Partial errors identify the original multipart filename, not its index.
    if (batch.length && (batch.length >= MAX_BATCH_FILES || bytes + entry.file.size > MAX_BATCH_BYTES || names.has(entry.file.name))) {
      yield batch
      batch = []
      bytes = 0
      names.clear()
    }
    batch.push(entry)
    bytes += entry.file.size
    names.add(entry.file.name)
  }
  if (batch.length) yield batch
}

function onFilePick(event: Event) {
  const input = event.target as HTMLInputElement
  const entries = Array.from(input.files || [], (file) => ({ id: ++nextUploadId, file }))
  input.value = ''
  void uploadFiles(entries)
}

function removeUploadFailure(id: number) {
  uploadFailures.value = uploadFailures.value.filter((entry) => entry.id !== id)
}

async function uploadFiles(entries: UploadEntry[]) {
  if (!entries.length || uploading.value) return
  const request = new AbortController()
  uploadRequest = request
  const weight = ({ file }: UploadEntry) => Math.max(1, file.size)
  const bytes = (batch: UploadEntry[]) => batch.reduce((sum, entry) => sum + weight(entry), 0)
  const total = bytes(entries)
  const retryIds = new Set(entries.map((entry) => entry.id))
  uploadFailures.value = uploadFailures.value.filter((entry) => !retryIds.has(entry.id))
  let processed = 0
  let uploaded = 0
  let failed = 0
  let completed = 0
  const fail = (entry: UploadEntry, reason: string) => {
    uploadFailures.value.push({ ...entry, reason })
    failed++
  }
  uploading.value = true
  uploadPct.value = 0
  uploadSummary.value = `上传中：${completed}/${entries.length} 张已处理`
  try {
    for (const batch of uploadBatches(entries)) {
      if (request.signal.aborted) return
      const batchBytes = bytes(batch)
      try {
        const result = await imageApi.upload(Number(props.id), batch.map(({ file }) => file), (fraction) => {
          uploadPct.value = Math.min(99, Math.floor(100 * (processed + batchBytes * fraction) / total))
        }, request.signal)
        if (request.signal.aborted) return
        const errors = new Map((result.partial_errors || []).map((error) => [error.filename, error.error]))
        const confirmed = batch.length - errors.size === result.items.length &&
          [...errors.keys()].every((name) => batch.some(({ file }) => file.name === name))
        for (const entry of batch) {
          if (errors.has(entry.file.name)) fail(entry, errors.get(entry.file.name) || '上传失败')
          else if (confirmed) uploaded++
          else fail(entry, '服务器未确认此文件的上传结果，请核对图片列表后重试')
        }
      } catch (e) {
        if (request.signal.aborted) return
        const reason = batch.length > 1 ? `该批未确认上传结果：${errMsg(e)}` : errMsg(e)
        for (const entry of batch) fail(entry, reason)
      }
      processed += batchBytes
      completed += batch.length
      uploadPct.value = Math.floor(100 * processed / total)
      uploadSummary.value = `上传中：${completed}/${entries.length} 张已处理 · 成功 ${uploaded} · 未成功 ${failed}`
    }
    await loadAll()
    if (!request.signal.aborted) uploadSummary.value = `上传处理完成：成功 ${uploaded} 张 · 未成功 ${failed} 张`
  } finally {
    if (uploadRequest === request) uploadRequest = undefined
    uploading.value = false
  }
}

async function onDeleteImage(img: ImageBrief) {
  try {
    await ElMessageBox.confirm(`删除图片「${img.original_filename}」？`, '确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    await imageApi.remove(img.id)
    await loadAll()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function onQualityCheck() {
  qcLoading.value = true
  try {
    const r = await imageApi.qualityCheck(Number(props.id))
    ElMessage.success(`质量检查完成：${r.checked} 张（OK ${r.ok} / 警告 ${r.warning} / 无效 ${r.invalid}）`)
    await loadAll()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    qcLoading.value = false
  }
}

async function onSubmitJob() {
  submitting.value = true
  try {
    await jobApi.create(Number(props.id), jobForm.value)
    ElMessage.success('任务已提交，请在任务详情页查看进度')
    jobDialogVisible.value = false
    await loadAll()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    submitting.value = false
  }
}

onMounted(loadAll)
onBeforeUnmount(() => uploadRequest?.abort())
</script>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.toolbar h3 { margin: 0; }
.spacer { flex: 1; }
.stats { margin-bottom: 16px; }
.stat { text-align: center; padding: 6px 0; }
.stat .num { font-size: 22px; font-weight: 600; color: #303133; }
.stat .label { font-size: 12px; color: #909399; margin-top: 4px; }
.section { margin-bottom: 16px; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.upload-summary { margin-bottom: 12px; color: var(--el-text-color-regular); font-size: 13px; }
.upload-failures { list-style: none; padding: 0; margin: 0 0 12px; }
.upload-failures li { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding: 6px 0; }
.upload-failures span { overflow-wrap: anywhere; color: var(--el-text-color-regular); }
.upload-failures .el-button { margin: 0; }
.thumb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 12px;
}
.thumb-item { text-align: center; }
.thumb-box {
  position: relative;
  width: 100%;
  aspect-ratio: 1;
  border-radius: 6px;
  overflow: hidden;
  background: #eef1f6;
  display: flex;
  align-items: center;
  justify-content: center;
}
.thumb-box img { width: 100%; height: 100%; object-fit: cover; }
.thumb-loading { color: #c0c4cc; font-size: 24px; }
.qtag { position: absolute; top: 4px; left: 4px; }
.thumb-name {
  font-size: 12px;
  color: #303133;
  margin-top: 6px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.thumb-meta { font-size: 11px; color: #909399; }
.empty { text-align: center; color: #909399; padding: 30px 0; }
.hint { font-size: 12px; color: #909399; margin-left: 10px; }
</style>
