import { ref } from 'vue'

const DB_NAME = 'adventurer_s1_reader_assets'
const DB_VERSION = 1
const STORE_NAME = 'assets'
const BACKGROUND_KEY = 'expedition_reader_background'
const MAX_SOURCE_BYTES = 5 * 1024 * 1024
const MAX_EDGE_PX = 2560
const WEBP_QUALITY = 0.86
const ALLOWED_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp'])

function openAssetDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION)
    request.onupgradeneeded = () => {
      const db = request.result
      if (!db.objectStoreNames.contains(STORE_NAME)) db.createObjectStore(STORE_NAME)
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error ?? new Error('无法打开本地素材存储'))
  })
}

async function readBackgroundBlob(): Promise<Blob | null> {
  const db = await openAssetDb()
  try {
    return await new Promise((resolve, reject) => {
      const request = db.transaction(STORE_NAME, 'readonly').objectStore(STORE_NAME).get(BACKGROUND_KEY)
      request.onsuccess = () => resolve(request.result instanceof Blob ? request.result : null)
      request.onerror = () => reject(request.error ?? new Error('读取本地背景失败'))
    })
  } finally {
    db.close()
  }
}

async function writeBackgroundBlob(blob: Blob | null): Promise<void> {
  const db = await openAssetDb()
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = db.transaction(STORE_NAME, 'readwrite')
      const store = transaction.objectStore(STORE_NAME)
      if (blob) store.put(blob, BACKGROUND_KEY)
      else store.delete(BACKGROUND_KEY)
      transaction.oncomplete = () => resolve()
      transaction.onerror = () => reject(transaction.error ?? new Error('保存本地背景失败'))
      transaction.onabort = () => reject(transaction.error ?? new Error('保存本地背景已取消'))
    })
  } finally {
    db.close()
  }
}

function canvasToBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(blob => {
      if (blob) {
        resolve(blob)
        return
      }
      canvas.toBlob(
        fallback => fallback ? resolve(fallback) : reject(new Error('浏览器无法处理这张图片')),
        'image/png',
      )
    }, 'image/webp', WEBP_QUALITY)
  })
}

async function loadImage(file: File): Promise<{
  source: CanvasImageSource
  width: number
  height: number
  dispose: () => void
}> {
  if (typeof createImageBitmap === 'function') {
    const bitmap = await createImageBitmap(file)
    return {
      source: bitmap,
      width: bitmap.width,
      height: bitmap.height,
      dispose: () => bitmap.close(),
    }
  }

  const url = URL.createObjectURL(file)
  const image = new Image()
  try {
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve()
      image.onerror = () => reject(new Error('无法读取这张图片'))
      image.src = url
    })
    return {
      source: image,
      width: image.naturalWidth,
      height: image.naturalHeight,
      dispose: () => URL.revokeObjectURL(url),
    }
  } catch (error) {
    URL.revokeObjectURL(url)
    throw error
  }
}

async function normalizeBackground(file: File): Promise<Blob> {
  if (!ALLOWED_TYPES.has(file.type)) throw new Error('仅支持 JPG、PNG 或 WebP 图片')
  if (file.size > MAX_SOURCE_BYTES) throw new Error('背景图片不能超过 5 MB')

  const image = await loadImage(file)
  try {
    if (image.width <= 0 || image.height <= 0) throw new Error('图片尺寸无效')
    const scale = Math.min(1, MAX_EDGE_PX / Math.max(image.width, image.height))
    const width = Math.max(1, Math.round(image.width * scale))
    const height = Math.max(1, Math.round(image.height * scale))
    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = height
    const context = canvas.getContext('2d')
    if (!context) throw new Error('浏览器无法处理这张图片')
    context.drawImage(image.source, 0, 0, width, height)
    return await canvasToBlob(canvas)
  } finally {
    image.dispose()
  }
}

export function useReaderBackground() {
  const backgroundUrl = ref<string | null>(null)
  const loading = ref(false)

  function replaceUrl(blob: Blob | null) {
    if (backgroundUrl.value) URL.revokeObjectURL(backgroundUrl.value)
    backgroundUrl.value = blob ? URL.createObjectURL(blob) : null
  }

  async function loadBackground() {
    loading.value = true
    try {
      replaceUrl(await readBackgroundBlob())
    } finally {
      loading.value = false
    }
  }

  async function importBackground(file: File) {
    loading.value = true
    try {
      const blob = await normalizeBackground(file)
      await writeBackgroundBlob(blob)
      replaceUrl(blob)
    } finally {
      loading.value = false
    }
  }

  async function clearBackground() {
    loading.value = true
    try {
      await writeBackgroundBlob(null)
      replaceUrl(null)
    } finally {
      loading.value = false
    }
  }

  function disposeBackground() {
    replaceUrl(null)
  }

  return {
    backgroundUrl,
    backgroundLoading: loading,
    loadBackground,
    importBackground,
    clearBackground,
    disposeBackground,
  }
}
