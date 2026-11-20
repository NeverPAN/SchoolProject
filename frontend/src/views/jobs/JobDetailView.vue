<template>
  <div v-if="job">
    <div class="toolbar">
      <el-button :icon="ArrowLeft" text @click="$router.back()">返回</el-button>
      <h3>{{ isRegionJob ? '区域提取任务' : '重建任务' }} #{{ job.id }}</h3>
      <el-tag :type="statusType(job.status)">{{ STATUS_LABELS[job.status] || job.status }}</el-tag>
      <div class="spacer" />
      <el-button
        v-if="['CREATED', 'QUEUED', 'RUNNING', 'VALIDATING', 'RESUMING'].includes(job.status)"
        type="danger" plain @click="onCancel"
      >取消任务</el-button>
      <el-button
        v-if="['FAILED', 'CANCELLED'].includes(job.status)"
        type="warning" plain @click="onResume"
      >{{ isRegionJob ? '重新入队' : '断点恢复' }}</el-button>
    </div>

    <el-row :gutter="16">
      <el-col :span="16">
        <el-card shadow="never" class="section">
          <template #header><span>{{ isRegionJob ? '区域提取（非局部重建）' : '执行状态（进度为阶段权重估算值）' }}</span></template>
          <el-progress
            :percentage="Math.round(job.progress)"
            :stroke-width="18"
            :status="job.status === 'FAILED' ? 'exception' : job.status === 'SUCCEEDED' ? 'success' : undefined"
            style="margin-bottom: 20px"
          />
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="当前阶段">{{ job.current_stage ? (STAGE_LABELS[job.current_stage] || job.current_stage) : '-' }}</el-descriptions-item>
            <el-descriptions-item :label="isRegionJob ? '任务模式' : '重建模式'">{{ isRegionJob ? '从已有网格裁剪导出' : job.pipeline_mode === 'mock' ? '模拟管线' : 'COLMAP 真实重建' }}</el-descriptions-item>
            <el-descriptions-item v-if="!isRegionJob" label="引擎版本">{{ job.engine_version || '-' }}</el-descriptions-item>
            <el-descriptions-item label="开始时间">{{ fmtTime(job.started_at) }}</el-descriptions-item>
            <el-descriptions-item label="结束时间">{{ fmtTime(job.finished_at) }}</el-descriptions-item>
            <template v-if="!isRegionJob">
              <el-descriptions-item label="GPU">{{ job.config?.use_gpu ? '启用' : '未启用' }}</el-descriptions-item>
              <el-descriptions-item label="质量预设">{{ { fast: '快速', standard: '标准', high: '高精度' }[job.config?.preset] || job.config?.preset }}</el-descriptions-item>
              <el-descriptions-item label="匹配策略">{{ { auto: '自动', exhaustive: '穷举', sequential: '顺序' }[job.config?.matcher] || job.config?.matcher }}</el-descriptions-item>
              <el-descriptions-item label="稠密/网格">{{ job.config?.enable_dense ? '开' : '关' }} / {{ job.config?.enable_mesh ? '开' : '关' }}</el-descriptions-item>
            </template>
          </el-descriptions>
          <el-alert
            v-if="job.status === 'FAILED'"
            type="error" :closable="false" style="margin-top: 12px"
            :title="isRegionJob ? '区域提取失败' : `失败阶段：${STAGE_LABELS[job.error_stage || ''] || job.error_stage || '-'}`"
            :description="`${job.error_code || ''}: ${job.error_message || ''}`"
            show-icon
          />
          <el-alert
            v-if="job.status === 'SUCCEEDED'"
            type="success" :closable="false" style="margin-top: 12px"
            :title="isRegionJob ? '区域提取完成，已导出独立模型' : '重建完成，可在项目页的模型列表中查看与下载模型'"
            show-icon
          />
          <router-link v-if="isRegionJob && job.status === 'SUCCEEDED'" class="result-link" :to="{ name: 'project-detail', params: { id: job.project_id } }">前往项目查看与下载结果</router-link>
          <el-alert
            v-if="textureFallback"
            type="warning" :closable="false" style="margin-top: 12px"
            title="未生成照片贴图" :description="textureFallback" show-icon
          />
        </el-card>

        <el-card v-if="!isRegionJob || allStages.length" shadow="never" class="section">
          <template #header><span>阶段时间线</span></template>
          <el-steps data-testid="job-stage-timeline" direction="vertical" style="max-height: 480px; overflow: auto">
            <el-step
              v-for="st in allStages"
              :key="st.stage"
              :title="STAGE_LABELS[st.stage] || st.stage"
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
              <span>任务日志</span>
              <el-select v-if="!isRegionJob" v-model="logStage" size="small" clearable placeholder="全部阶段" style="width: 150px">
                <el-option v-for="(label, key) in STAGE_LABELS" :key="key" :label="label" :value="key" />
              </el-select>
            </div>
          </template>
          <pre class="logs">{{ logs }}</pre>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowLeft } from '@element-plus/icons-vue'
import { jobApi, fmtTime, errMsg, STAGE_LABELS, STATUS_LABELS, type Job, type JobEvent } from '../../api'

const props = defineProps<{ id: string }>()
const job = ref<Job | null>(null)
const logs = ref('')
const logStage = ref<string | null>(null)
let timer: number | undefined
const isRegionJob = computed(() => job.value?.pipeline_mode === 'local_update')

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
  return STAGE_ORDER.filter((s) => byStage[s]?.length || s === current).map((s) => {
    const evs = byStage[s] || []
    const latest = evs[evs.length - 1]
    const durations = evs.filter((e) => e.duration_ms != null).map((e) => e.duration_ms!)
    const dur = durations.length ? durations.reduce((a, b) => a + b, 0) : null
    let description = latest?.message || ''
    if (dur != null) description += `（${(dur / 1000).toFixed(1)}s）`
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
  timer = window.setInterval(async () => {
    await load()
    await loadLogs()
    const s = job.value?.status
    if (s === 'SUCCEEDED' || s === 'FAILED' || s === 'CANCELLED') {
      window.clearInterval(timer)
    }
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
