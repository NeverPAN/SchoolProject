<template>
  <div v-if="job">
    <div class="toolbar">
      <el-button :icon="ArrowLeft" text @click="$router.back()">返回</el-button>
      <h3>{{ title }} #{{ job.id }}</h3>
      <el-tag :type="statusType(job.status)">{{ isArchive && job.status === 'SUCCEEDED' ? '已归档' : STATUS_LABELS[job.status] || job.status }}</el-tag>
      <div class="spacer" />
      <el-button
        v-if="!isArchive && ['CREATED', 'QUEUED', 'RUNNING', 'VALIDATING', 'RESUMING'].includes(job.status)"
        type="danger" plain @click="onCancel"
      >取消任务</el-button>
      <el-button
        v-if="!isArchive && !isLocalReconstruct && ['FAILED', 'CANCELLED'].includes(job.status)"
        type="warning" plain @click="onResume"
      >{{ isRegionJob ? '重新入队' : '断点恢复' }}</el-button>
    </div>

    <el-row :gutter="16">
      <el-col :span="16">
        <el-card shadow="never" class="section">
          <template #header><span>{{ sectionTitle }}</span></template>
          <el-progress
            v-if="!isArchive"
            :percentage="Math.round(job.progress)"
            :stroke-width="18"
            :status="job.status === 'FAILED' ? 'exception' : job.status === 'SUCCEEDED' ? 'success' : undefined"
            style="margin-bottom: 20px"
          />
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item v-if="!isArchive" label="当前阶段">{{ job.current_stage ? stageLabel(job.current_stage) : '-' }}</el-descriptions-item>
            <el-descriptions-item v-if="job.experiment_name" label="版本名称">{{ job.experiment_name }}</el-descriptions-item>
            <el-descriptions-item label="任务模式">{{ modeLabel }}</el-descriptions-item>
            <el-descriptions-item v-if="!isRegionJob" label="引擎版本">{{ job.engine_version || (isArchive ? '未知（未留存）' : '-') }}</el-descriptions-item>
            <el-descriptions-item :label="isArchive ? '原始运行开始' : '开始时间'">{{ runTime(job.started_at) }}</el-descriptions-item>
            <el-descriptions-item :label="isArchive ? '原始运行结束' : '结束时间'">{{ runTime(job.finished_at) }}</el-descriptions-item>
            <template v-if="!isRegionJob && !isArchive && !isLocalReconstruct">
              <el-descriptions-item label="GPU">{{ job.config?.use_gpu ? '启用' : '未启用' }}</el-descriptions-item>
              <el-descriptions-item label="质量预设">{{ { fast: '快速', standard: '标准', high: '高精度' }[job.config?.preset] || job.config?.preset }}</el-descriptions-item>
              <el-descriptions-item label="匹配策略">{{ { auto: '自动', exhaustive: '穷举', sequential: '顺序' }[job.config?.matcher] || job.config?.matcher }}</el-descriptions-item>
              <el-descriptions-item label="稠密/网格">{{ job.config?.enable_dense ? '开' : '关' }} / {{ job.config?.enable_mesh ? '开' : '关' }}</el-descriptions-item>
            </template>
          </el-descriptions>
          <el-alert
            v-if="job.status === 'FAILED'"
            type="error" :closable="false" style="margin-top: 12px"
            :title="isRegionJob ? '区域提取失败' : `失败阶段：${job.error_stage ? stageLabel(job.error_stage) : '-'}`"
            :description="`${job.error_code || ''}: ${job.error_message || ''}`"
            show-icon
          />
          <el-alert
            v-if="job.status === 'SUCCEEDED'"
            type="success" :closable="false" style="margin-top: 12px"
            :title="isArchive ? '实验产物已归档，仅供对照，不代表重新运行' : isRegionJob ? '区域提取完成，已导出独立模型' : isLocalReconstruct ? '新增照片局部重建完成，已生成独立点云候选（未替换原模型或网格）' : '重建完成，可在项目页的模型列表中查看与下载模型'"
            show-icon
          />
          <router-link v-if="(isRegionJob || isLocalReconstruct) && job.status === 'SUCCEEDED'" class="result-link" :to="{ name: 'project-detail', params: { id: job.project_id } }">前往项目查看与下载结果</router-link>
          <el-alert
            v-if="textureFallback"
            type="warning" :closable="false" style="margin-top: 12px"
            title="未生成照片贴图" :description="textureFallback" show-icon
          />
        </el-card>

        <el-card v-if="frozenConfig" shadow="never" class="section">
          <template #header><span>冻结配置（任务创建时确定，恢复不改变）</span></template>
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item v-for="(value, key) in frozenResolved" :key="key" :label="resolvedLabel(String(key))">{{ value }}</el-descriptions-item>
          </el-descriptions>
          <p v-if="job.config?.matcher_decision" class="ver-desc">{{ job.config.matcher_decision }}</p>
          <template v-if="frozenPreflight">
            <el-divider />
            <span class="label">提交预检</span>
            <el-descriptions :column="3" border size="small" style="margin-top: 8px">
              <el-descriptions-item label="图片数">{{ frozenPreflight.image_count }}</el-descriptions-item>
              <el-descriptions-item label="总字节数">{{ fmtSize(Number(frozenPreflight.total_bytes || 0)) }}</el-descriptions-item>
              <el-descriptions-item label="视角重叠">{{ frozenPreflight.view_overlap === 'unknown_until_registration' ? '注册后判定' : frozenPreflight.view_overlap }}</el-descriptions-item>
              <el-descriptions-item v-for="(count, status) in frozenPreflight.quality_summary || {}" :key="status" :label="`质量 ${status}`">{{ count }}</el-descriptions-item>
            </el-descriptions>
            <el-alert
              v-for="(warning, index) in frozenPreflight.warnings || []" :key="index"
              type="warning" :closable="false" style="margin-top: 8px" :title="String(warning)" show-icon
            />
          </template>
          <template v-if="frozenCapability">
            <el-divider />
            <span class="label">GPU 能力核查</span>
            <el-descriptions :column="3" border size="small" style="margin-top: 8px">
              <el-descriptions-item label="CUDA COLMAP">{{ frozenCapability.colmap_has_cuda ? '可用' : '不可用' }}</el-descriptions-item>
              <el-descriptions-item label="请求 GPU">{{ (frozenCapability.requested_gpu_index || []).join(', ') || '-' }}</el-descriptions-item>
              <el-descriptions-item label="可见 GPU">{{ frozenCapability.visible_gpu_count ?? '未知（无法探测）' }}</el-descriptions-item>
            </el-descriptions>
          </template>
        </el-card>

        <el-card v-if="!isRegionJob || allStages.length" shadow="never" class="section">
          <template #header><span>{{ isArchive ? '已留存的阶段证据' : '阶段时间线' }}</span></template>
          <el-empty v-if="isArchive && !allStages.length" description="未留存可归属到本实验的阶段记录" :image-size="64" />
          <el-steps v-else data-testid="job-stage-timeline" direction="vertical" style="max-height: 480px; overflow: auto">
            <el-step
              v-for="st in allStages"
              :key="st.stage"
              :title="stageLabel(st.stage)"
              :description="st.description"
              :status="st.stepStatus"
            />
          </el-steps>
        </el-card>
      </el-col>

      <el-col :span="8">
        <el-card shadow="never" class="section">
          <template #header>
            <div class="card-head">
              <span>{{ isArchive ? '已留存的日志' : '任务日志' }}</span>
              <el-select v-if="!isRegionJob" v-model="logStage" size="small" clearable placeholder="任务日志" style="width: 150px" @change="loadLogs">
                <el-option v-for="stage in logStages" :key="stage" :label="stageLabel(stage)" :value="stage" />
              </el-select>
            </div>
          </template>
          <pre class="logs">{{ logs || (isArchive ? '未留存此项日志；可查看已记录阶段。' : '(暂无日志)') }}</pre>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowLeft } from '@element-plus/icons-vue'
import { jobApi, fmtTime, fmtSize, errMsg, STAGE_LABELS, STATUS_LABELS, type Job, type JobEvent } from '../../api'

const props = defineProps<{ id: string }>()
const job = ref<Job | null>(null)
const logs = ref('')
const logStage = ref<string | null>(null)
let timer: number | undefined
const isRegionJob = computed(() => job.value?.pipeline_mode === 'local_update')
const isLocalReconstruct = computed(() => job.value?.pipeline_mode === 'local_reconstruct')
const isGeneralLocal = computed(() => job.value?.pipeline_mode === 'local_general')
const isMeshPatch = computed(() => job.value?.pipeline_mode === 'local_mesh')

const frozenConfig = computed(() => {
  const config = job.value?.config
  return config && !isArchive.value && !isRegionJob.value ? config : null
})
const frozenResolved = computed<Record<string, unknown>>(() => (job.value?.config?.resolved as Record<string, unknown>) || {})
const frozenPreflight = computed(() => (job.value?.config?.preflight || null) as { image_count?: number; total_bytes?: number; view_overlap?: string; quality_summary?: Record<string, number>; warnings?: string[] } | null)
const frozenCapability = computed(() => (job.value?.config?.capability || null) as { colmap_has_cuda?: boolean; requested_gpu_index?: number[]; visible_gpu_count?: number | null } | null)
const RESOLVED_LABELS: Record<string, string> = {
  max_features: 'SIFT 特征上限', matcher: '匹配策略', poisson_depth: 'Poisson 深度', poisson_threads: 'Poisson 线程',
  poisson_trim: 'Poisson trim', dense_max_image_size: '稠密最大边长', patchmatch_iterations: 'PatchMatch 迭代',
  fusion_threads: '融合线程', texture_start_scale: '贴图起始档', gpu_index: 'GPU 索引',
}
function resolvedLabel(key: string) {
  return RESOLVED_LABELS[key] || key
}
const isArchive = computed(() => job.value?.pipeline_mode === 'experiment')
const title = computed(() => isArchive.value ? '实验归档' : isRegionJob.value ? '区域提取任务' :
  isLocalReconstruct.value ? '新增照片局部重建任务' : isGeneralLocal.value ? '通用局部更新任务' :
  isMeshPatch.value ? '局部网格补丁任务' : '重建任务')
const sectionTitle = computed(() => isArchive.value ? '离线实验留存记录' : isRegionJob.value ? '区域提取（非局部重建）' :
  isLocalReconstruct.value ? '新增照片局部重建（仅生成点云候选）' :
  isGeneralLocal.value ? '通用局部更新（注册 + 局部融合点云候选）' :
  isMeshPatch.value ? '局部网格补丁（父侧几何/贴图原样保留，失败即拒绝发布）' : '执行状态（进度为阶段权重估算值）')
const modeLabel = computed(() => isArchive.value ? '实验归档（只读）' : isRegionJob.value ? '区域提取：从已有网格裁剪导出' :
  isLocalReconstruct.value ? '新增照片局部重建（点云候选）' :
  isGeneralLocal.value ? '通用局部更新：任意新增照片注册到父模型（点云候选）' :
  isMeshPatch.value ? '局部网格补丁：源点云候选网格化并与父网格缝合' : job.value?.pipeline_mode === 'mock' ? '模拟管线' : 'COLMAP 真实重建')

function runTime(value: string | null) {
  if (!isArchive.value) return fmtTime(value)
  // The archive preserves the database's naive UTC values, not local wall clock time.
  return value ? `${value.replace('T', ' ')} UTC` : '未知（未留存）'
}

function stageLabel(stage: string) {
  if (stage === 'LOCAL_UPDATE') return '区域提取'
  if (stage === 'REGISTER_NEW_VIEWS') return '新增照片注册'
  if (stage === 'LOCAL_MESH_CUT') return '父网格切割'
  if (stage === 'LOCAL_MESHING') return '补丁网格化'
  if (stage === 'LOCAL_STITCH') return '边界缝合'
  if (stage === 'VALIDATION') return '发布前校验'
  return STAGE_LABELS[stage] || stage
}

const LOCAL_GENERAL_ORDER = ['IMAGE_VALIDATION', 'REGISTER_NEW_VIEWS', 'IMAGE_UNDISTORTION',
  'DENSE_RECONSTRUCTION', 'STEREO_FUSION', 'EXPORTING']

const MESH_PATCH_ORDER = ['IMAGE_VALIDATION', 'LOCAL_MESH_CUT', 'LOCAL_MESHING', 'LOCAL_STITCH', 'VALIDATION', 'EXPORTING']

const STAGE_ORDER = [
  'IMAGE_VALIDATION', 'FEATURE_EXTRACTION', 'FEATURE_MATCHING', 'SPARSE_RECONSTRUCTION',
  'IMAGE_UNDISTORTION', 'DENSE_RECONSTRUCTION', 'STEREO_FUSION', 'MESHING', 'TEXTURING', 'EXPORTING',
]

const textureFallback = computed(() => {
  const events = job.value?.events?.filter((event) => event.stage === 'TEXTURING') || []
  const outcome = [...events].reverse().find((event) =>
    event.status !== 'running' && !(event.status === 'skipped' && event.message?.includes('检查点有效')),
  )
  if (!outcome || !['skipped', 'warning'].includes(outcome.status)) return ''
  return outcome.message || '任务未生成照片贴图，请查看可用的导出模型。'
})

const allStages = computed(() => {
  const events: JobEvent[] = job.value?.events || []
  const byStage: Record<string, JobEvent[]> = {}
  for (const e of events) (byStage[e.stage] ||= []).push(e)
  const current = job.value?.current_stage
  const order = isRegionJob.value ? ['LOCAL_UPDATE'] : isGeneralLocal.value ? LOCAL_GENERAL_ORDER :
    isMeshPatch.value ? MESH_PATCH_ORDER : STAGE_ORDER
  return order.filter((s) => byStage[s]?.length || s === current).map((s) => {
    const evs = byStage[s] || []
    const latest = evs[evs.length - 1]
    const durations = evs.filter((e) => e.duration_ms != null).map((e) => e.duration_ms!)
    const dur = durations.length ? durations.reduce((a, b) => a + b, 0) : null
    let description = latest?.message || ''
    if (isArchive.value && latest?.started_at && latest.finished_at) {
      description += `\n${runTime(latest.started_at)} → ${runTime(latest.finished_at)}`
    }
    if (dur != null) description += `（${(dur / 1000).toFixed(1)}s）`
    else if (isArchive.value) description += '（耗时未知）'
    let stepStatus: 'wait' | 'process' | 'finish' | 'error' = 'wait'
    if (latest?.status === 'completed') stepStatus = 'finish'
    else if (latest?.status === 'failed') stepStatus = 'error'
    else if (latest?.status === 'cancelled') stepStatus = 'error'
    else if (latest?.status === 'running') stepStatus = 'process'
    else if (latest?.status === 'skipped') stepStatus = 'finish'
    else if (job.value?.current_stage === s) stepStatus = 'process'
    return { stage: s, description: description || ' ', stepStatus }
  })
})

const logStages = computed(() => isArchive.value ? allStages.value.map(({ stage }) => stage) : Object.keys(STAGE_LABELS))

function isSettled() {
  return !!job.value && (isArchive.value || ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(job.value.status))
}

function statusType(s: string) {
  if (s === 'SUCCEEDED') return 'success'
  if (s === 'FAILED') return 'danger'
  if (s === 'CANCELLED' || s === 'CANCEL_REQUESTED') return 'info'
  return 'warning'
}

async function load() {
  try {
    job.value = await jobApi.get(Number(props.id))
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function loadLogs() {
  try {
    logs.value = await jobApi.logs(Number(props.id), logStage.value || undefined)
  } catch {
    logs.value = '(日志暂不可用)'
  }
}

async function onCancel() {
  try {
    await jobApi.cancel(Number(props.id))
    ElMessage.success('取消请求已提交')
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

async function onResume() {
  try {
    await jobApi.resume(Number(props.id))
    ElMessage.success(isRegionJob.value ? '区域提取任务已重新入队' : '任务已重新入队，将从有效检查点恢复')
    await load()
  } catch (e) {
    ElMessage.error(errMsg(e))
  }
}

onMounted(async () => {
  await load()
  await loadLogs()
  if (isSettled()) return
  timer = window.setInterval(async () => {
    await load()
    await loadLogs()
    if (isSettled()) window.clearInterval(timer)
  }, 2000)
})

onBeforeUnmount(() => window.clearInterval(timer))
</script>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.toolbar h3 { margin: 0; }
.spacer { flex: 1; }
.section { margin-bottom: 16px; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.logs {
  background: #1e1e28;
  color: #9fd3a8;
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  padding: 12px;
  border-radius: 6px;
  max-height: 560px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}
</style>
