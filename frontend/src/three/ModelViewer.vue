<template>
  <div ref="container" class="viewer-container">
    <div v-if="loading || pending" class="overlay" role="status">
      <el-icon class="is-loading" :size="34"><Loading /></el-icon>
      <div class="overlay-text">模型加载中…</div>
    </div>
    <div v-else-if="error || fetchError" class="overlay error" role="alert">
      <el-icon :size="34"><WarningFilled /></el-icon>
      <div class="overlay-text">{{ error || fetchError }}</div>
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
  pending?: boolean
  fetchError?: string
  fileFormat: string // 'glb' | 'ply'
  style: StylePreset
  displayUp?: [number, number, number] | null
  legacySrgbVertexColors?: boolean
  background: BackgroundPreset
}>()

const emit = defineEmits<{
  (e: 'loaded', info: { vertices: number; faces: number; points: number; sizeLabel: string }): void
}>()

const container = ref<HTMLDivElement>()
const loading = ref(false)
const error = ref('')

let renderer: THREE.WebGLRenderer | null = null
let scene: THREE.Scene | null = null
let camera: THREE.PerspectiveCamera | null = null
let controls: OrbitControls | null = null
let pivot: THREE.Group | null = null
const baseRotation = new THREE.Quaternion()
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
  renderer.setSize(el.clientWidth, el.clientHeight, false)
  renderer.domElement.classList.add('model-canvas')
  renderer.domElement.setAttribute('aria-label', '三维模型画布')
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
  if (!el || !renderer || !camera || !el.clientWidth || !el.clientHeight) return
  camera.aspect = el.clientWidth / el.clientHeight
  camera.updateProjectionMatrix()
  renderer.setSize(el.clientWidth, el.clientHeight, false)
  if (pivot) fitCameraTo(pivot, true)
}

// ---------------------------------------------------------------------------
// 模型加载
// ---------------------------------------------------------------------------

function clearModel() {
  if (!modelRoot || !scene) return
  if (pivot) scene.remove(pivot)
  pivot = null
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
    const original = obj.userData.originalMaterial
    if (original && original !== mat) (Array.isArray(original) ? original : [original]).forEach((m: THREE.Material) => m.dispose())
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

function fitCameraTo(root: THREE.Object3D, resize = false) {
  if (!camera || !controls) return
  const box = new THREE.Box3().setFromObject(root)
  if (box.isEmpty()) return
  const size = box.getSize(new THREE.Vector3())
  const center = box.getCenter(new THREE.Vector3())
  const maxDim = Math.max(size.x, size.y, size.z) || 1
  const vHalfFov = THREE.MathUtils.degToRad(camera.fov / 2)
  const hHalfFov = Math.atan(Math.tan(vHalfFov) * camera.aspect)
  const radius = size.length() / 2 || 0.5
  const dist = 1.12 * radius / Math.sin(Math.min(vHalfFov, hHalfFov))
  const dir = new THREE.Vector3(0.7, 0.45, 1).normalize()
  const oldHomeDist = homeView.pos.distanceTo(homeView.target)
  if (resize && oldHomeDist > 0) {
    camera.position.sub(controls.target).multiplyScalar(dist / oldHomeDist).add(controls.target)
  } else {
    camera.position.copy(center).addScaledVector(dir, dist)
    controls.target.copy(center)
  }
  camera.near = maxDim / 1000
  camera.far = dist * 20
  camera.updateProjectionMatrix()
  controls.update()
  homeView.pos.copy(center).addScaledVector(dir, dist)
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
  const group = new THREE.Group()
  const hasColors = !!geo.getAttribute('color')

  // PLY 面数据存在 → 网格；否则点云
  if (geo.index) {
    const material = new THREE.MeshStandardMaterial({
      vertexColors: hasColors,
      color: hasColors ? 0xffffff : 0xbfc8d1,
      side: THREE.DoubleSide,
    })
    const mesh = new THREE.Mesh(geo, material)
    mesh.name = 'ply_mesh'
    group.add(mesh)
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
  try {
    if (!renderer) {
      try {
        initScene()
      } catch (e) {
        if (!renderer) throw new Error('当前浏览器无法启动 WebGL，仍可下载模型。')
        throw e
      }
    }
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
    if (!container.value || !scene) {
      disposeObject(root)
      return
    }
    modelRoot = root
    const counts = computeCounts(root)
    prepareMaterials(root, fmt === 'glb' && !!props.legacySrgbVertexColors)
    const center = new THREE.Box3().setFromObject(root).getCenter(new THREE.Vector3())
    pivot = new THREE.Group()
    pivot.add(root)
    root.position.sub(center)
    pivot.position.copy(center)
    scene!.add(pivot)
    const up = new THREE.Vector3(...(props.displayUp ?? [0, 1, 0]))
    baseRotation.setFromUnitVectors(up.normalize(), new THREE.Vector3(0, 1, 0))
    restoreOrientation()
    applyStyle()
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

function prepareMaterials(root: THREE.Object3D, fixLegacyColor: boolean) {
  const corrected = new Set<THREE.BufferGeometry>()
  root.traverse((obj) => {
    if (!(obj instanceof THREE.Mesh)) return
    const geo = obj.geometry as THREE.BufferGeometry
    if (!geo.getAttribute('normal')) geo.computeVertexNormals()
    const colors = geo.getAttribute('color')
    if (fixLegacyColor && colors && !corrected.has(geo)) {
      corrected.add(geo)
      const linear = new Float32Array(colors.count * colors.itemSize)
      for (let i = 0; i < colors.count; i++) {
        const c = new THREE.Color(colors.getX(i), colors.getY(i), colors.getZ(i)).convertSRGBToLinear()
        const j = i * colors.itemSize
        linear[j] = c.r
        linear[j + 1] = c.g
        linear[j + 2] = c.b
        if (colors.itemSize === 4) linear[j + 3] = colors.getW(i)
      }
      geo.setAttribute('color', new THREE.BufferAttribute(linear, colors.itemSize))
    }
    const materials = Array.isArray(obj.material) ? obj.material : [obj.material]
    for (const mat of materials) {
      mat.side = THREE.DoubleSide
      if (mat instanceof THREE.MeshStandardMaterial) mat.flatShading = false
      mat.needsUpdate = true
    }
  })
}

function applyStyle() {
  if (!modelRoot) return
  const meshes: THREE.Mesh[] = []
  modelRoot.traverse((obj) => {
    if (obj instanceof THREE.Mesh) meshes.push(obj)
    if (obj instanceof THREE.Points && !obj.userData.previewPoints) {
      const mat = obj.material as THREE.PointsMaterial
      mat.vertexColors = !!obj.geometry.getAttribute('color') && !['morandi', 'monochrome'].includes(props.style)
      mat.color.setHex(mat.vertexColors ? 0xffffff : props.style === 'morandi' ? 0xa8beb2 : 0x9ea6b0)
      mat.needsUpdate = true
    }
  })
  for (const mesh of meshes) {
    const state = mesh.userData
    if (!state.originalMaterial) state.originalMaterial = mesh.material
    if (state.styleMaterial) {
      state.styleMaterial.dispose()
      state.styleMaterial = null
    }
    mesh.material = state.originalMaterial
    if (props.style === 'pointcloud' && !state.points) {
      const colors = !!mesh.geometry.getAttribute('color')
      const geo = new THREE.BufferGeometry()
      geo.setAttribute('position', mesh.geometry.getAttribute('position').clone())
      if (colors) geo.setAttribute('color', mesh.geometry.getAttribute('color').clone())
      const points = new THREE.Points(geo, new THREE.PointsMaterial({
        size: 2, sizeAttenuation: false, vertexColors: colors, color: colors ? 0xffffff : 0xaab2bd,
      }))
      points.userData.previewPoints = true
      points.position.copy(mesh.position)
      points.quaternion.copy(mesh.quaternion)
      points.scale.copy(mesh.scale)
      mesh.parent?.add(points)
      state.points = points
    }
    mesh.visible = props.style !== 'pointcloud'
    if (state.points) state.points.visible = !mesh.visible
    if (['morandi', 'monochrome', 'wireframe'].includes(props.style)) {
      state.styleMaterial = new THREE.MeshStandardMaterial({
        side: THREE.DoubleSide, wireframe: props.style === 'wireframe',
        color: props.style === 'morandi' ? MORANDI_PALETTE[mesh.id % MORANDI_PALETTE.length] : 0x9ea6b0,
      })
      mesh.material = state.styleMaterial
    }
  }
}

function rotateOrientation(axis: 'x' | 'z', degrees: number) {
  if (!pivot) return
  pivot.rotateOnWorldAxis(axis === 'x' ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 0, 1), THREE.MathUtils.degToRad(degrees))
  fitCameraTo(pivot)
}

function restoreOrientation() {
  if (!pivot) return
  pivot.quaternion.copy(baseRotation)
  fitCameraTo(pivot)
}

function resetView() {
  if (!camera || !controls) return
  camera.position.copy(homeView.pos)
  controls.target.copy(homeView.target)
  controls.update()
}

defineExpose({ resetView, rotateOrientation, restoreOrientation })

// ---------------------------------------------------------------------------
// 生命周期
// ---------------------------------------------------------------------------

onMounted(() => {
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
  border-radius: 8px;
  overflow: hidden;
}
.viewer-container :deep(.model-canvas) { display: block; width: 100%; height: 100%; }
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
