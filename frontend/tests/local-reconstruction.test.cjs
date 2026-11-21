const test = require('node:test')
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { resolve } = require('node:path')
const { parse, compileScript } = require('vue/compiler-sfc')
const ts = require('typescript')
const { createRenderer, nextTick } = require('vue')

const filename = 'ModelViewerView.vue'
const source = readFileSync(resolve(__dirname, '../src/views/models/ModelViewerView.vue'), 'utf8')
const { descriptor } = parse(source, { filename })
const script = compileScript(descriptor, { id: 'local-reconstruction-test' }).content
const { outputText } = ts.transpileModule(script, { compilerOptions: { module: ts.ModuleKind.CommonJS } })
const setup = new Function('require', 'exports', outputText)
const flush = async () => { await new Promise(setImmediate); await nextTick() }

async function mountViewer({ available, imageIds = [71, 72, 73], listFails = false, submitFails = false, metadata = {} }) {
  const bounds = { min: [0, 0, 0], max: [4, 4, 4] }
  const calls = []
  const exports = {}
  setup((path) => ({
    '../../three/ModelViewer.vue': { default: {} },
    'vue-router': { useRoute: () => ({ query: {} }) },
    'element-plus': { ElMessage: {} },
    '@element-plus/icons-vue': {},
    '../../api': {
      imageApi: {
        list: async (projectId) => {
          calls.push(['photos', projectId])
          if (listFails) { listFails = false; throw Error('photos unavailable') }
          return [71, 72, 73, 91].map((id) => ({ id, original_filename: `renamed-${id}.JPG` }))
        },
      },
      modelApi: {
        get: async (id) => ({ id, project_id: 5, format: 'glb', model_type: 'mesh',
          local_reconstruction_available: available, local_reconstruction_bounds: bounds, local_reconstruction_image_ids: imageIds, ...metadata }),
        fetchFile: async () => new ArrayBuffer(0),
        saveRegion: async (id, region) => { calls.push(['save', id, JSON.parse(JSON.stringify(region))]); return { id: 8 } },
        createLocalReconstruction: async (id, regionId, imageIds) => {
          calls.push(['reconstruct', id, regionId, imageIds])
          if (submitFails) { submitFails = false; throw Error('queue unavailable') }
          return { id: 19 }
        },
        createRegionExtraction: async (id, regionId) => { calls.push(['extract', id, regionId]); return { id: 20 } },
        createLocalMesh: async (id) => { calls.push(['mesh-patch', id]); return { id: 21 } },
      },
      errMsg: (error) => error.message,
      isPhotoTexture: (model) => model.format?.toLowerCase() === 'glb' &&
        (model.stage_origin === 'PHOTO_TEXTURE_UPRIGHT' || model.stage_origin === 'PHOTO_TEXTURE'),
      photoColorAvailable: (model) => {
        const assets = model.texture_assets
        if (assets) return assets.embedded_textures === true && assets.primitives_with_uv > 0
        return model.format?.toLowerCase() === 'glb' &&
          (model.stage_origin === 'PHOTO_TEXTURE_UPRIGHT' || model.stage_origin === 'PHOTO_TEXTURE')
      },
    },
  }[path] ?? require(path)), exports)
  const { createApp } = createRenderer({ createComment: () => ({}), insert() {}, remove() {} })
  const app = createApp({ ...exports.default, render: () => null }, { id: '24' })
  const oldWindow = global.window
  global.window = new EventTarget()
  app.mount({})
  await flush()
  const view = app._instance.setupState
  view.onLoaded({ vertices: 20, faces: 12, points: 0, sizeLabel: '4 × 4 × 4' })
  return { view, calls, bounds, cleanup() { app.unmount(); global.window = oldWindow } }
}

test('calibrated reconstruction separates photo and region selection from extraction and reuses the saved region on failure', async () => {
  const { view, calls, bounds, cleanup } = await mountViewer({ available: true, listFails: true, submitFails: true })
  try {
    view.enterSelection('reconstruction')
    await flush()
    assert.match(view.photosError, /photos unavailable/)
    await view.loadProjectPhotos()
    assert.deepEqual(view.projectPhotos.map((photo) => photo.id), [71, 72, 73])
    view.setRegionSelection({ bbox_min: [-1, 0, 0], bbox_max: [2, 2, 2] })
    assert.match(view.selectionError, /超出已验证范围/)
    view.useValidatedRegion()
    assert.equal(view.canSubmitRegion, false)
    view.selectedPhotoIds = [71, 72, 91]
    assert.equal(view.canSubmitRegion, false)
    view.selectedPhotoIds = [71, 72, 73]
    assert.equal(view.canSubmitRegion, true)
    await view.submitRegion()
    assert.match(view.selectionError, /queue unavailable/)
    assert.equal(view.savedRegionId, 8)
    await view.submitRegion()
    assert.equal(view.regionJobId, 19)
    assert.equal(view.createdJobLabel, '局部重建')
    assert.equal(view.selectMode, false)
    assert.deepEqual(calls, [
      ['photos', 5], ['photos', 5],
      ['save', 24, { bbox_min: bounds.min, bbox_max: bounds.max, note: '局部重建选区' }],
      ['reconstruct', 24, 8, [71, 72, 73]], ['reconstruct', 24, 8, [71, 72, 73]],
    ])
  } finally {
    cleanup()
  }
})

test('omitted capability still allows extraction without loading or sending photos', async () => {
  const { view, calls, cleanup } = await mountViewer({})
  try {
    view.enterSelection('reconstruction')
    assert.equal(view.selectMode, false)
    view.enterSelection('extraction')
    const region = { bbox_min: [-1, 0, 0], bbox_max: [2, 2, 2] }
    view.setRegionSelection(region)
    await view.submitRegion()
    assert.equal(view.regionJobId, 20)
    assert.equal(view.createdJobLabel, '区域提取')
    assert.deepEqual(calls, [['save', 24, { ...region, note: '查看器框选' }], ['extract', 24, 8]])
  } finally {
    cleanup()
  }
})

test('an empty eligible-ID list does not offer other project photos', async () => {
  const { view, cleanup } = await mountViewer({ available: true, imageIds: [] })
  try {
    view.enterSelection('reconstruction')
    await flush()
    assert.deepEqual(view.projectPhotos, [])
    assert.equal(view.hasRequiredPhotos, false)
    view.exitSelection()
    assert.equal(view.selectMode, false)
  } finally {
    cleanup()
  }
})


test('archive mesh displays artifact counts, not a misleading zero point count or initial file counters', async () => {
  const { view, cleanup } = await mountViewer({ metadata: {
    model_type: 'web', pipeline_mode: 'experiment', experiment: { code: 'R01', parent: 'C01' },
    vertex_count: 307895, face_count: 449617, point_count: 0,
  } })
  try {
    assert.equal(view.isArchive, true)
    assert.deepEqual(view.geometryRows, [{ label: '网格顶点数', value: 307895 }, { label: '三角面数', value: 449617 }])
    assert.match(view.recordTime('2026-11-20T13:54:27'), /13:54:27/)
    assert.equal(view.recordTime(null), '未知（未留存）')
  } finally { cleanup() }
})

test('fused point-cloud candidate can submit a mesh patch job directly', async () => {
  const { view, calls, cleanup } = await mountViewer({ metadata: {
    model_type: 'dense_pointcloud', format: 'ply', stage_origin: 'LOCAL_POINT_FUSION',
    vertex_count: 100, face_count: 0, point_count: 100,
  } })
  try {
    await view.submitMeshPatch()
    assert.equal(view.regionJobId, 21)
    assert.equal(view.createdJobLabel, '局部网格补丁')
    assert.deepEqual(calls, [['mesh-patch', 24]])
  } finally { cleanup() }
})

test('photo-color availability follows measured GLB assets, not stage origin alone', async () => {
  const { view, cleanup } = await mountViewer({ metadata: {
    model_type: 'web', format: 'glb', stage_origin: 'LOCAL_REGION',
    texture_assets: { primitives_total: 2, primitives_with_uv: 2, materials_total: 2,
      materials_with_base_color_texture: 2, texture_count: 1, embedded_textures: true,
      external_texture_uris: [], double_sided_materials: 2 },
  } })
  try {
    assert.equal(view.availableStyles.some((s) => s.key === 'photo-color'), true)
  } finally { cleanup() }
})

test('untextured GLB does not offer original-color mode even with photo origin', async () => {
  const { view, cleanup } = await mountViewer({ metadata: {
    model_type: 'web', format: 'glb', stage_origin: 'PHOTO_TEXTURE_UPRIGHT',
    texture_assets: { primitives_total: 1, primitives_with_uv: 0, materials_total: 1,
      materials_with_base_color_texture: 0, texture_count: 0, embedded_textures: false,
      external_texture_uris: [], double_sided_materials: 0 },
  } })
  try {
    assert.equal(view.availableStyles.some((s) => s.key === 'photo-color'), false)
  } finally { cleanup() }
})

test('legacy models without texture assets keep stage-origin fallback', async () => {
  const { view, cleanup } = await mountViewer({ metadata: {
    model_type: 'web', format: 'glb', stage_origin: 'PHOTO_TEXTURE_UPRIGHT',
  } })
  try {
    assert.equal(view.availableStyles.some((s) => s.key === 'photo-color'), true)
  } finally { cleanup() }
})

test('fused point-cloud metadata does not list mesh vertices or faces', async () => {
  const { view, cleanup } = await mountViewer({ metadata: {
    model_type: 'dense_pointcloud', format: 'ply', vertex_count: 4258813, face_count: 0, point_count: 4258813,
  } })
  try {
    assert.equal(view.isArchive, false)
    assert.deepEqual(view.geometryRows, [{ label: '点云采样点数', value: 4258813 }])
  } finally { cleanup() }
})
