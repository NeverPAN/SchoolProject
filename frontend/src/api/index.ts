import { api, errMsg } from './client'

// ---------- 类型 ----------

export interface User {
  id: number
  username: string
  role: string
  created_at: string
}

export interface ImageBrief {
  id: number
  original_filename: string
  file_size: number
  width: number
  height: number
  format: string
  quality_status: string
  has_thumbnail: boolean
}

export interface Project {
  id: number
  name: string
  description: string
  status: string
  owner_id: number
  created_at: string
  updated_at: string
  image_count: number
  total_image_bytes: number
  model_count: number
  job_count: number
}

export interface ProjectDetail extends Project {
  images: ImageBrief[]
  quality_summary: Record<string, number>
}

export interface ImageDetail extends ImageBrief {
  project_id: number
  stored_name: string
  checksum: string
  exif: Record<string, any> | null
  quality_notes: string | null
  created_at: string
}

export interface JobEvent {
  id: number
  stage: string
  status: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  message: string | null
}

export interface Job {
  experiment_name?: string | null
  id: number
  project_id: number
  status: string
  current_stage: string | null
  progress: number
  pipeline_mode: string
  engine_version: string | null
  config: {
    preset: string
    matcher: string
    enable_dense: boolean
    enable_mesh: boolean
    use_gpu: boolean
    mode: string
  }
  started_at: string | null
  finished_at: string | null
  error_stage: string | null
  error_code: string | null
  error_message: string | null
  created_at: string
  events?: JobEvent[]
}

export interface ModelVersion {
  id: number
  version_number: number
  parent_version_id: number | null
  change_type: string
  change_description: string
  created_at: string
}

export interface ModelItem {
  id: number
  project_id: number
  job_id: number | null
  model_type: string
  format: string
  file_size: number
  vertex_count: number
  face_count: number
  point_count: number
  stage_origin: string | null
  created_at: string
  versions?: ModelVersion[]
}

export interface ModelDetail extends ModelItem {
  experiment?: { code: string; parent: string | null; scope: string } | null
  experiment_name?: string | null
  pipeline_mode?: string | null
  display_up?: [number, number, number] | null
  legacy_srgb_vertex_colors: boolean
}

// ---------- Auth ----------

export const authApi = {
  async register(username: string, password: string): Promise<User> {
    const r = await api.post('/auth/register', { username, password })
    return r.data
  },
  async login(username: string, password: string): Promise<{ access_token: string; user: User }> {
    const r = await api.post('/auth/login', { username, password })
    return r.data
  },
  async me(): Promise<User> {
    const r = await api.get('/me')
    return r.data
  },
}

// ---------- Projects ----------

export const projectApi = {
  async list(): Promise<Project[]> {
    const r = await api.get('/projects')
    return r.data
  },
  async create(name: string, description: string): Promise<Project> {
    const r = await api.post('/projects', { name, description })
    return r.data
  },
  async get(id: number): Promise<ProjectDetail> {
    const r = await api.get(`/projects/${id}`)
    return r.data
  },
  async update(id: number, patch: { name?: string; description?: string }): Promise<Project> {
    const r = await api.patch(`/projects/${id}`, patch)
    return r.data
  },
  async remove(id: number): Promise<void> {
    await api.delete(`/projects/${id}`)
  },
}

// ---------- Images ----------

interface ImageUploadResult {
  items: ImageDetail[]
  partial_errors: { filename: string; error: string; code: string }[]
}

export const imageApi = {
  async upload(projectId: number, files: File[], onProgress: (fraction: number) => void, signal?: AbortSignal): Promise<ImageUploadResult> {
    const form = new FormData()
    for (const f of files) form.append('files', f)
    const r = await api.post<ImageDetail[] | ImageUploadResult>(`/projects/${projectId}/images`, form, {
      signal,
      onUploadProgress: (e) => {
        if (e.total) onProgress(Math.min(1, e.loaded / e.total))
      },
    })
    return Array.isArray(r.data) ? { items: r.data, partial_errors: [] } : r.data
  },
  async list(projectId: number): Promise<ImageDetail[]> {
    const r = await api.get(`/projects/${projectId}/images`)
    return r.data
  },
  async remove(id: number): Promise<void> {
    await api.delete(`/images/${id}`)
  },
  async qualityCheck(projectId: number): Promise<{ checked: number; ok: number; warning: number; invalid: number }> {
    const r = await api.post(`/projects/${projectId}/quality-check`)
    return r.data
  },
  async thumbnailUrl(id: number): Promise<string> {
    const r = await api.get(`/images/${id}/thumbnail`, { responseType: 'blob' })
    return URL.createObjectURL(r.data)
  },
}

// ---------- Jobs ----------

export const jobApi = {
  async create(projectId: number, opts: { preset: string; matcher: string; enable_dense: boolean; enable_mesh: boolean; experiment_name?: string }): Promise<Job> {
    const r = await api.post(`/projects/${projectId}/jobs`, opts)
    return r.data
  },
  async list(projectId: number): Promise<Job[]> {
    const r = await api.get(`/projects/${projectId}/jobs`)
    return r.data
  },
  async get(id: number): Promise<Job> {
    const r = await api.get(`/jobs/${id}`)
    return r.data
  },
  async logs(id: number, stage?: string): Promise<string> {
    const r = await api.get(`/jobs/${id}/logs`, { params: stage ? { stage } : {}, responseType: 'text' })
    return r.data as unknown as string
  },
  async cancel(id: number): Promise<void> {
    await api.post(`/jobs/${id}/cancel`)
  },
  async resume(id: number): Promise<void> {
    await api.post(`/jobs/${id}/resume`)
  },
}

// ---------- Models ----------

export const modelApi = {
  async list(projectId: number): Promise<ModelItem[]> {
    const r = await api.get(`/projects/${projectId}/models`)
    return r.data
  },
  async get(id: number): Promise<ModelDetail> {
    const r = await api.get(`/models/${id}`)
    return r.data
  },
  async download(id: number): Promise<void> {
    const r = await api.get(`/models/${id}/download`, { responseType: 'blob' })
    const url = URL.createObjectURL(r.data)
    const a = document.createElement('a')
    a.href = url
    a.download = `model_${id}.${String(r.headers['content-type'] ?? '').includes('gltf') ? 'glb' : 'ply'}`
    a.click()
    URL.revokeObjectURL(url)
  },
  async fetchFile(id: number): Promise<ArrayBuffer> {
    const r = await api.get(`/models/${id}/file`, { responseType: 'arraybuffer' })
    return r.data
  },
  async previewUrl(id: number): Promise<string> {
    const r = await api.get(`/models/${id}/preview`, { responseType: 'blob' })
    return URL.createObjectURL(r.data)
  },
  async remove(id: number): Promise<void> {
    await api.delete(`/models/${id}`)
  },
  // 阶段 8 原型：保存查看器框选区域，并创建区域提取任务
  async saveRegion(
    id: number,
    payload: { bbox_min: number[]; bbox_max: number[]; camera?: Record<string, unknown>; note?: string },
  ): Promise<{ id: number }> {
    const r = await api.post(`/models/${id}/regions`, payload)
    return r.data
  },
  async createRegionExtraction(id: number, regionId: number): Promise<Job> {
    const r = await api.post<Job>(`/models/${id}/local-update`, { region_id: regionId })
    return r.data
  },
}

export { errMsg }

export function isPhotoTexture(model: ModelItem): boolean {
  return model.format.toLowerCase() === 'glb' &&
    (model.stage_origin === 'PHOTO_TEXTURE_UPRIGHT' || model.stage_origin === 'PHOTO_TEXTURE')
}

export function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`
}

export function fmtTime(s: string | null): string {
  if (!s) return '-'
  return new Date(s).toLocaleString('zh-CN', { hour12: false })
}

export const STAGE_LABELS: Record<string, string> = {
  IMAGE_VALIDATION: '图片校验',
  FEATURE_EXTRACTION: '特征提取',
  FEATURE_MATCHING: '特征匹配',
  SPARSE_RECONSTRUCTION: '稀疏重建',
  IMAGE_UNDISTORTION: '图像去畸变',
  DENSE_RECONSTRUCTION: '稠密重建',
  STEREO_FUSION: '点云融合',
  MESHING: '网格生成',
  TEXTURING: '照片贴图',
  EXPORTING: '模型导出',
}

export const STATUS_LABELS: Record<string, string> = {
  CREATED: '已创建',
  VALIDATING: '校验中',
  QUEUED: '排队中',
  RUNNING: '运行中',
  SUCCEEDED: '成功',
  FAILED: '失败',
  CANCEL_REQUESTED: '取消中',
  CANCELLED: '已取消',
  RESUMING: '恢复中',
}
