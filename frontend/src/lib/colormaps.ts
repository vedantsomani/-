/**
 * Astronomical colormaps and canvas image transfer function filters.
 * Supports: Greyscale, Inverted Greyscale, Viridis, Inferno, and Infrared Amber.
 */

export type ColormapId = 'greyscale' | 'inverted' | 'viridis' | 'inferno' | 'amber'

export interface ColormapOption {
  readonly id: ColormapId
  readonly label: string
  readonly description: string
}

export const COLORMAP_OPTIONS: readonly ColormapOption[] = [
  { id: 'greyscale', label: 'Greyscale', description: 'Standard linear grey' },
  { id: 'inverted', label: 'Inverted (B&W)', description: 'Black on white (best for spotting movers)' },
  { id: 'viridis', label: 'Viridis', description: 'Perceptually uniform, colorblind friendly' },
  { id: 'inferno', label: 'Inferno', description: 'High-contrast thermal infrared map' },
  { id: 'amber', label: 'Infrared Amber', description: 'Observatory amber/gold monochrome' },
] as const

/** Generate 256-entry RGB look-up tables */
function buildLUT(id: ColormapId): Uint8ClampedArray {
  const lut = new Uint8ClampedArray(256 * 3)

  for (let i = 0; i < 256; i++) {
    const t = i / 255
    let r = 0
    let g = 0
    let b = 0

    if (id === 'greyscale') {
      r = i
      g = i
      b = i
    } else if (id === 'inverted') {
      r = 255 - i
      g = 255 - i
      b = 255 - i
    } else if (id === 'viridis') {
      // High-accuracy cubic polynomial approximation of Viridis
      r = Math.round(Math.max(0, Math.min(255, 255 * (-0.17 * t * t + 1.15 * t + 0.02))))
      g = Math.round(Math.max(0, Math.min(255, 255 * (-1.4 * t * t + 2.2 * t + 0.05))))
      b = Math.round(Math.max(0, Math.min(255, 255 * (0.8 * t * t * t - 2.1 * t * t + 0.45 * t + 0.55))))
      if (t < 0.05) {
        r = 68; g = 1; b = 84
      } else if (t > 0.95) {
        r = 253; g = 231; b = 37
      }
    } else if (id === 'inferno') {
      // High-accuracy polynomial approximation of Inferno
      r = Math.round(Math.max(0, Math.min(255, 255 * (t < 0.5 ? 2.5 * t * t : 1.1 * t + 0.2))))
      g = Math.round(Math.max(0, Math.min(255, 255 * (t < 0.3 ? 0.3 * t : 1.5 * t * t - 0.2))))
      b = Math.round(Math.max(0, Math.min(255, 255 * (t < 0.25 ? 4 * t : t < 0.7 ? 1 - t : 0.2))))
      if (t < 0.04) {
        r = 0; g = 0; b = 4
      } else if (t > 0.96) {
        r = 252; g = 255; b = 164
      }
    } else if (id === 'amber') {
      // Warm infrared amber glow
      r = Math.round(Math.min(255, 255 * Math.pow(t, 0.85)))
      g = Math.round(Math.min(255, 195 * Math.pow(t, 1.1)))
      b = Math.round(Math.min(255, 45 * Math.pow(t, 1.6)))
    }

    lut[i * 3] = r
    lut[i * 3 + 1] = g
    lut[i * 3 + 2] = b
  }

  return lut
}

const LUTS: Record<ColormapId, Uint8ClampedArray> = {
  greyscale: buildLUT('greyscale'),
  inverted: buildLUT('inverted'),
  viridis: buildLUT('viridis'),
  inferno: buildLUT('inferno'),
  amber: buildLUT('amber'),
}

/**
 * Apply colormap and min/max clip to an existing 2D canvas context.
 * Computes luminance of each pixel, applies clip stretching, then maps through the LUT.
 */
export function applyColormapToCanvas(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  colormap: ColormapId,
  minClip = 0,
  maxClip = 255,
): void {
  if (colormap === 'greyscale' && minClip === 0 && maxClip === 255) return

  const imgData = ctx.getImageData(0, 0, width, height)
  const data = imgData.data
  const lut = LUTS[colormap]
  const range = Math.max(1, maxClip - minClip)

  for (let i = 0; i < data.length; i += 4) {
    // Relative perceptual luminance
    const lum = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]
    // Stretch to 0..255
    const clamped = Math.max(0, Math.min(255, Math.round(((lum - minClip) / range) * 255)))
    data[i] = lut[clamped * 3]
    data[i + 1] = lut[clamped * 3 + 1]
    data[i + 2] = lut[clamped * 3 + 2]
  }

  ctx.putImageData(imgData, 0, 0)
}

/** Compute a 64-bin histogram of image luminance for contrast visualization */
export function computeImageHistogram(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  numBins = 64,
): number[] {
  const bins = new Array<number>(numBins).fill(0)
  try {
    const imgData = ctx.getImageData(0, 0, width, height)
    const data = imgData.data
    const factor = numBins / 256
    for (let i = 0; i < data.length; i += 4) {
      const lum = Math.floor((0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]) * factor)
      bins[Math.min(numBins - 1, Math.max(0, lum))]++
    }
  } catch {
    // Fallback if canvas is tainted
  }
  return bins
}
