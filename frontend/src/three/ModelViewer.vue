<template>
  <div ref="container" class="viewer-container">
    <div v-if="loading" class="overlay">
      <el-icon class="is-loading" :size="34"><Loading /></el-icon>
      <div class="overlay-text">模型加载中… {{ Math.round(progress) }}%</div>
    </div>
    <div v-else-if="error" class="overlay error">
      <el-icon :size="34"><WarningFilled /></el-icon>
      <div class="overlay-text">{{ error }}</div>
    </div>
    <div class="hud">
      <slot name="hud" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { PLYLoader } from 'three/examples/jsm/loaders/PLYLoader.js'
import { Loading, WarningFilled } from '@element-plus/icons-vue'

export type StylePreset = 'original' | 'morandi' | 'monochrome' | 'wireframe' | 'pointcloud'
export type BackgroundPreset = 'light' | 'dark' | 'gradient'

const props = defineProps<{
  fileData: ArrayBuffer | null
  fileFormat: string // 'glb' | 'ply'
  style: StylePreset
  background: BackgroundPreset
}>()

const emit = defineEmits<{
  (e: 'loaded', info: { vertices: number; faces: number; points: number; sizeLabel: string }): void
}>()

const container = ref<HTMLDivElement>()
const loading = ref(false)
const progress = ref(0)
const error = ref('')

let renderer: THREE.WebGLRenderer | null = null
let scene: THREE.Scene | null = null
let camera: THREE.PerspectiveCamera | null = null
let controls: OrbitControls | null = null
let modelRoot: THREE.Group | null = null
let frameId = 0
let resizeObserver: ResizeObserver | null = null
let homeView = { pos: new THREE.Vector3(), target: new THREE.Vector3() }

const MORANDI_PALETTE = [0xa8beb2, 0xb8a8a0, 0x93a3b3, 0xc4b8ad, 0x9aa8a0, 0xb3ab9c]

// ---------------------------------------------------------------------------
// 初始化场景
// ---------------------------------------------------------------------------

function initScene() {
  const el = container.value!
  renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: false })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
  renderer.setSize(el.clientWidth, el.clientHeight)
  el.appendChild(renderer.domElement)

  scene = new THREE.Scene()
  applyBackground()

  camera = new THREE.PerspectiveCamera(50, el.clientWidth / el.clientHeight, 0.01, 5000)
  camera.position.set(3, 3, 5)

  controls = new OrbitControls(camera, renderer.domElement)
  controls.enableDamping = true
  controls.dampingFactor = 0.08

  // 灯光
  const hemi = new THREE.HemisphereLight(0xffffff, 0x8d8d8d, 1.1)
  scene.add(hemi)
  const dir = new THREE.DirectionalLight(0xffffff, 1.6)
  dir.position.set(5, 10, 7)
  scene.add(dir)

  const grid = new THREE.GridHelper(10, 20, 0xb0b8c4, 0xd8dde4)
  grid.name = '__grid'
  scene.add(grid)

  resizeObserver = new ResizeObserver(onResize)
  resizeObserver.observe(el)

  const animate = () => {
    frameId = requestAnimationFrame(animate)
    controls?.update()
    if (renderer && scene && camera) renderer.render(scene, camera)
  }
  animate()
}

function applyBackground() {
  if (!scene) return
  const bg = props.background
  if (bg === 'dark') {
    scene.background = new THREE.Color(0x1a2230)
  } else if (bg === 'gradient') {
    const canvas = document.createElement('canvas')
    canvas.width = 2
    canvas.height = 256
    const ctx = canvas.getContext('2d')!
    const grad = ctx.createLinearGradient(0, 0, 0, 256)
    grad.addColorStop(0, '#e8eef4')
    grad.addColorStop(1, '#c3d0dc')
    ctx.fillStyle = grad
    ctx.fillRect(0, 0, 2, 256)
    const tex = new THREE.CanvasTexture(canvas)
    scene.background = tex
  } else {
    scene.background = new THREE.Color(0xf2f4f7)
  }
}

function onResize() {
  const el = container.value
  if (!el || !renderer || !camera) return
  camera.aspect = el.clientWidth / el.clientHeight
  camera.updateProjectionMatrix()
  renderer.setSize(el.clientWidth, el.clientHeight)
}

// ---------------------------------------------------------------------------
// 模型加载
// ---------------------------------------------------------------------------

function clearModel() {
  if (!modelRoot || !scene) return
  scene.remove(modelRoot)
  disposeObject(modelRoot)
  modelRoot = null
}

function disposeObject(root: THREE.Object3D) {
  root.traverse((obj) => {
    const mesh = obj as THREE.Mesh
    if (mesh.geometry) mesh.geometry.dispose()
    const mat = (mesh as any).material
    if (Array.isArray(mat)) mat.forEach((m: THREE.Material) => m.dispose())
    else if (mat) mat.dispose()
  })
}

function computeCounts(root: THREE.Object3D) {
  let vertices = 0
  let faces = 0
  let points = 0
  root.traverse((obj) => {
    const mesh = obj as THREE.Mesh
    const geo = (mesh as any).geometry as THREE.BufferGeometry | undefined
    if (!geo) return
    if ((obj as any).isPoints) {
      points += geo.attributes.position?.count ?? 0
    } else if (geo.index) {
      vertices += geo.attributes.position?.count ?? 0
      faces += geo.index.count / 3
    } else {
      vertices += geo.attributes.position?.count ?? 0
      faces += (geo.attributes.position?.count ?? 0) / 3
    }
  })
  return { vertices, faces: Math.round(faces), points }
}

function fitCameraTo(root: THREE.Object3D) {
  if (!camera || !controls) return
  const box = new THREE.Box3().setFromObject(root)
  if (box.isEmpty()) return
  const size = box.getSize(new THREE.Vector3())
  const center = box.getCenter(new THREE.Vector3())
  const maxDim = Math.max(size.x, size.y, size.z) || 1
  const fov = (camera.fov * Math.PI) / 180
  const dist = (maxDim / 2) / Math.tan(fov / 2) * 1.6
  const dir = new THREE.Vector3(0.7, 0.45, 1).normalize()
  camera.position.copy(center).addScaledVector(dir, dist)
  camera.near = maxDim / 1000
  camera.far = dist * 20
  camera.updateProjectionMatrix()
  controls.target.copy(center)
  controls.update()
  homeView.pos.copy(camera.position)
  homeView.target.copy(center)

  // 网格大小适配
  const grid = scene?.getObjectByName('__grid') as THREE.GridHelper | undefined
  if (grid) {
    grid.scale.setScalar(maxDim / 10)
    grid.position.y = box.min.y
  }
}

function buildFromGLB(data: ArrayBuffer) {
  return new Promise<THREE.Group>((resolve, reject) => {
    const loader = new GLTFLoader()
    loader.parse(
      data,
      '',
      (gltf) => resolve(gltf.scene as THREE.Group),
      (err) => reject(new Error(`GLB 解析失败: ${err?.message || err}`)),
    )
  })
}

function buildFromPLY(data: ArrayBuffer): THREE.Group {
  const loader = new PLYLoader()
  const geo = loader.parse(data)
  geo.computeVertexNormals()
  const group = new THREE.Group()
  const hasColors = !!geo.getAttribute('color')
  const hasNormals = !!geo.getAttribute('normal') && geo.getAttribute('normal')!.itemSize === 3

  // PLY 面数据存在 → 网格；否则点云
  if (geo.index || (geo.attributes.position && !geo.getAttribute('normal') && false)) {
    const material = new THREE.MeshStandardMaterial({
      vertexColors: hasColors,
      color: hasColors ? 0xffffff : 0xbfc8d1,
      side: THREE.DoubleSide,
      flatShading: !hasNormals,
    })
    const mesh = new THREE.Mesh(geo, material)
    mesh.name = 'ply_mesh'
    group.add(mesh)
    const pts = new THREE.Points(
      geo,
      new THREE.PointsMaterial({ size: 0.0, visible: false }),
    )
    pts.visible = false
    pts.name = 'ply_hidden_points'
    group.add(pts)
  } else {
    const material = new THREE.PointsMaterial({
      size: 0.02,
      vertexColors: hasColors,
      color: hasColors ? 0xffffff : 0xbfc8d1,
      sizeAttenuation: true,
    })
    const pts = new THREE.Points(geo, material)
    pts.name = 'ply_points'
    group.add(pts)
  }
  return group
}

async function loadModel() {
  if (!props.fileData || !container.value) return
  loading.value = true
  error.value = ''
  progress.value = 0
  await new Promise((r) => setTimeout(r, 30))
  try {
    if (!renderer) initScene()
    clearModel()
    let root: THREE.Group
    const fmt = props.fileFormat.toLowerCase()
    if (fmt === 'glb' || fmt === 'gltf') {
      root = await buildFromGLB(props.fileData)
    } else if (fmt === 'ply') {
      root = buildFromPLY(props.fileData)
    } else {
      throw new Error(`不支持的模型格式: ${fmt}`)
    }
    modelRoot = root
    scene!.add(root)
    fitCameraTo(root)
    applyStyle()
    const counts = computeCounts(root)
    const box = new THREE.Box3().setFromObject(root)
    const size = box.getSize(new THREE.Vector3())
    emit('loaded', {
      vertices: counts.vertices,
      faces: counts.faces,
      points: counts.points,
      sizeLabel: `${size.x.toFixed(2)} × ${size.y.toFixed(2)} × ${size.z.toFixed(2)}`,
    })
  } catch (e: any) {
    error.value = e?.message || '模型加载失败'
  } finally {
    loading.value = false
  }
}

// ---------------------------------------------------------------------------
// 风格预设（仅修改查看器材质，不修改源模型文件）
// ---------------------------------------------------------------------------

function applyStyle() {
  if (!modelRoot) return
  const style = props.style
  modelRoot.traverse((obj) => {
    const mesh = obj as THREE.Mesh
    const geo = (mesh as any).geometry as THREE.BufferGeometry | undefined
    if (!geo) return
    const isPoints = (obj as any).isPoints as boolean

    if (isPoints) {
      const mat = obj.material as THREE.PointsMaterial
      if (obj.name === 'ply_hidden_points') {
        obj.visible = style === 'pointcloud'
        mat.size = 0.015
        return
      }
      // 独立点云：风格只改颜色/大小
      obj.visible = true
      if (style === 'monochrome') {
        mat.vertexColors = false
        mat.color = new THREE.Color(0x8b8f96)
        mat.size = 0.02
      } else if (style === 'morandi') {
        mat.vertexColors = false
        mat.color = new THREE.Color(0xa8beb2)
        mat.size = 0.02
      } else {
        mat.vertexColors = true
        mat.color = new THREE.Color(0xffffff)
        mat.size = 0.02
      }
      return
    }

    // GLB / PLY 网格
    const mat = obj.material as THREE.MeshStandardMaterial
    const hasVertexColors = !!geo.getAttribute('color')

    if (style === 'pointcloud') {
      // 网格隐藏，用点显示
      if (!obj.userData.__points) {
        const pts = new THREE.Points(
          geo,
          new THREE.PointsMaterial({ size: 0.012, vertexColors: hasVertexColors, color: hasVertexColors ? 0xffffff : 0xaab2bd }),
        )
        pts.name = '__style_points'
        obj.userData.__points = pts
        obj.parent?.add(pts)
        pts.position.copy(obj.position)
        pts.rotation.copy(obj.rotation)
        pts.scale.copy(obj.scale)
      }
      obj.userData.__points.visible = true
      obj.visible = false
      return
    }
    if (obj.userData.__points) obj.userData.__points.visible = false
    obj.visible = true

    mat.wireframe = style === 'wireframe'
    if (style === 'original') {
      mat.vertexColors = hasVertexColors && obj.name !== 'ply_mesh' ? true : hasVertexColors
      mat.color = new THREE.Color(hasVertexColors ? 0xffffff : 0xbfc8d1)
      mat.metalness = 0
      mat.roughness = 0.85
      if (obj.name.startsWith('Scene') || obj.userData.__isGltf) mat.vertexColors = hasVertexColors
    } else if (style === 'morandi') {
      mat.vertexColors = false
      mat.color = new THREE.Color(MORANDI_PALETTE[(mesh.id | 0) % MORANDI_PALETTE.length])
      mat.metalness = 0
      mat.roughness = 0.9
    } else if (style === 'monochrome') {
      mat.vertexColors = false
      mat.color = new THREE.Color(0x9ea6b0)
      mat.metalness = 0
      mat.roughness = 0.8
    } else if (style === 'wireframe') {
      mat.vertexColors = hasVertexColors
      mat.color = new THREE.Color(hasVertexColors ? 0xffffff : 0x7d8691)
    }
  })
}

function resetView() {
  if (!camera || !controls) return
  camera.position.copy(homeView.pos)
  controls.target.copy(homeView.target)
  controls.update()
}

defineExpose({ resetView })

// ---------------------------------------------------------------------------
// 生命周期
// ---------------------------------------------------------------------------

onMounted(() => {
  initScene()
  if (props.fileData) loadModel()
})

watch(
  () => props.fileData,
  () => loadModel(),
)

watch(
  () => props.style,
  () => applyStyle(),
)

watch(
  () => props.background,
  () => applyBackground(),
)

onBeforeUnmount(() => {
  cancelAnimationFrame(frameId)
  resizeObserver?.disconnect()
  clearModel()
  controls?.dispose()
  renderer?.dispose()
  if (renderer?.domElement.parentElement) {
    renderer.domElement.parentElement.removeChild(renderer.domElement)
  }
  renderer = null
  scene = null
})
</script>

<style scoped>
.viewer-container {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 480px;
  border-radius: 8px;
  overflow: hidden;
}
.overlay {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  background: rgba(245, 247, 250, 0.92);
  z-index: 10;
  color: #606266;
}
.overlay.error { color: #c45656; }
.overlay-text { font-size: 14px; }
.hud {
  position: absolute;
  right: 12px;
  top: 12px;
  z-index: 5;
}
</style>
