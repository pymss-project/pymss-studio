<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const TILE_CSS_WIDTH = 1024
const MAX_TILE_BUFFER_WIDTH = 4096

const props = defineProps<{
  peaks: number[]
  channelPeaks?: number[][]
  channels?: number
  assetDuration: number
  offset?: number
  duration: number
  width: number
  height: number
  fadeIn?: number
  fadeOut?: number
  color?: string
  progress?: number
}>()

const rootEl = ref<HTMLElement | null>(null)
const dpr = computed(() => Math.min(window.devicePixelRatio || 1, 2))
const tileCount = computed(() => Math.max(1, Math.ceil(Math.max(1, props.width) / TILE_CSS_WIDTH)))
const tileIndexes = computed(() => Array.from({ length: tileCount.value }, (_, index) => index))
const resolvedChannels = computed(() => {
  const peakChannels = props.channelPeaks?.filter(channel => channel.length).length || 0
  return Math.max(Number(props.channels || 0), peakChannels)
})

function resolveColor(): string {
  if (props.color) return props.color
  const styles = getComputedStyle(document.documentElement)
  return styles.getPropertyValue('--primary').trim() || '#7aa2ff'
}

function drawTile(canvas: HTMLCanvasElement, tileIndex: number) {
  const ctx = canvas.getContext('2d')
  if (!ctx) return

  const totalWidth = Math.max(1, Math.floor(props.width))
  const totalHeight = Math.max(1, Math.floor(props.height))
  const tileLeft = tileIndex * TILE_CSS_WIDTH
  const tileWidth = Math.max(1, Math.min(TILE_CSS_WIDTH, totalWidth - tileLeft))
  const ratio = dpr.value
  const targetW = Math.max(1, Math.min(MAX_TILE_BUFFER_WIDTH, Math.floor(tileWidth * ratio)))
  const targetH = Math.max(1, Math.floor(totalHeight * ratio))
  const scaleX = targetW / Math.max(1, tileWidth)

  if (canvas.width !== targetW) canvas.width = targetW
  if (canvas.height !== targetH) canvas.height = targetH
  canvas.style.width = `${tileWidth}px`
  canvas.style.height = `${totalHeight}px`
  canvas.style.left = `${tileLeft}px`

  ctx.setTransform(scaleX, 0, 0, ratio, 0, 0)
  ctx.clearRect(0, 0, tileWidth, totalHeight)

  const channels = resolvedChannels.value >= 2 && props.channelPeaks?.length
    ? props.channelPeaks.filter((channel) => channel.length).slice(0, 2)
    : [props.peaks]
  const primaryPeaks = channels[0] || []
  if (!primaryPeaks.length || props.assetDuration <= 0 || props.duration <= 0) return

  const total = primaryPeaks.length
  const startRatio = Math.max(0, Math.min(1, Number(props.offset || 0) / props.assetDuration))
  const endRatio = Math.max(startRatio, Math.min(1, (Number(props.offset || 0) + props.duration) / props.assetDuration))
  const startIdx = Math.min(total - 1, Math.floor(startRatio * total))
  const endIdx = Math.min(total, Math.ceil(endRatio * total))
  const span = Math.max(1, endIdx - startIdx)
  const color = resolveColor()
  // One column per device pixel draws a continuous outline at the true amplitude.
  const step = 1 / scaleX

  ctx.fillStyle = color

  const drawChannel = (peaks: number[], top: number, height: number) => {
    const mid = top + height / 2
    for (let localX = 0; localX < tileWidth; localX += step) {
      const globalX = tileLeft + localX
      const colStart = startIdx + Math.floor((globalX / totalWidth) * span)
      const colEnd = startIdx + Math.floor(((globalX + step) / totalWidth) * span)
      let peak = 0

      for (let i = colStart; i < Math.max(colStart + 1, colEnd); i += 1) {
        const value = peaks[i] || 0
        if (value > peak) peak = value
      }

      let envelope = 1
      const tSec = (globalX / totalWidth) * props.duration
      if (props.fadeIn && props.fadeIn > 0 && tSec < props.fadeIn) {
        envelope = Math.min(envelope, tSec / props.fadeIn)
      }
      if (props.fadeOut && props.fadeOut > 0 && tSec > props.duration - props.fadeOut) {
        envelope = Math.min(envelope, (props.duration - tSec) / props.fadeOut)
      }

      const barHeight = Math.max(1 / ratio, Math.min(1, peak) * envelope * (height * 0.8))
      ctx.fillRect(localX, mid - barHeight / 2, step, barHeight)
    }
  }

  if (channels.length >= 2) {
    const laneGap = 3
    const channelHeight = Math.max(1, (totalHeight - laneGap) / 2)
    drawChannel(channels[0], 0, channelHeight)
    drawChannel(channels[1], channelHeight + laneGap, channelHeight)
    ctx.fillStyle = 'rgba(128,128,128,0.12)'
    ctx.fillRect(0, channelHeight + laneGap / 2, tileWidth, 1 / ratio)
  } else {
    drawChannel(primaryPeaks, 0, totalHeight)
  }

  const fadeInWidth = props.fadeIn && props.duration > 0 ? Math.min(totalWidth, (props.fadeIn / props.duration) * totalWidth) : 0
  const fadeOutWidth = props.fadeOut && props.duration > 0 ? Math.min(totalWidth, (props.fadeOut / props.duration) * totalWidth) : 0

  if (fadeInWidth > tileLeft) {
    const localFadeEnd = Math.min(tileWidth, fadeInWidth - tileLeft)
    if (localFadeEnd > 1) {
      const gradient = ctx.createLinearGradient(0, 0, localFadeEnd, 0)
      gradient.addColorStop(0, 'rgba(255,255,255,0.16)')
      gradient.addColorStop(1, 'rgba(255,255,255,0)')
      ctx.fillStyle = gradient
      ctx.fillRect(0, 0, localFadeEnd, totalHeight)
    }
  }

  if (fadeOutWidth > 1) {
    const fadeOutStart = totalWidth - fadeOutWidth
    const localFadeStart = Math.max(0, fadeOutStart - tileLeft)
    const localFadeEnd = Math.min(tileWidth, totalWidth - tileLeft)
    const localFadeWidth = localFadeEnd - localFadeStart
    if (localFadeWidth > 1) {
      const gradient = ctx.createLinearGradient(localFadeStart, 0, localFadeStart + localFadeWidth, 0)
      gradient.addColorStop(0, 'rgba(255,255,255,0)')
      gradient.addColorStop(1, 'rgba(255,255,255,0.16)')
      ctx.fillStyle = gradient
      ctx.fillRect(localFadeStart, 0, localFadeWidth, totalHeight)
    }
  }
}

function draw() {
  const root = rootEl.value
  if (!root) return
  const canvases = root.querySelectorAll<HTMLCanvasElement>('.editor-waveform__tile')
  canvases.forEach((canvas) => {
    const index = Number(canvas.dataset.tileIndex || 0)
    drawTile(canvas, index)
  })
}

let frame: number | null = null

function scheduleDraw() {
  if (frame !== null) cancelAnimationFrame(frame)
  frame = requestAnimationFrame(() => {
    frame = null
    draw()
  })
}

watch(
  () => [props.peaks, props.channelPeaks, props.channels, props.assetDuration, props.offset, props.duration, props.width, props.height, props.fadeIn, props.fadeOut, props.color, tileCount.value],
  scheduleDraw,
  { deep: false },
)

onMounted(scheduleDraw)

onBeforeUnmount(() => {
  if (frame !== null) cancelAnimationFrame(frame)
})
</script>

<template>
  <div ref="rootEl" class="editor-waveform" :style="{ width: `${width}px`, height: `${height}px` }">
    <canvas
      v-for="tileIndex in tileIndexes"
      :key="tileIndex"
      class="editor-waveform__tile"
      :data-tile-index="tileIndex"
    />
  </div>
</template>

<style scoped>
.editor-waveform {
  position: relative;
  display: block;
  height: 100%;
  opacity: 0.9;
  overflow: hidden;
}

.editor-waveform__tile {
  position: absolute;
  top: 0;
  display: block;
  height: 100%;
}
</style>
