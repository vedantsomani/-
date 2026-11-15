/**
 * Lightweight client-side GIF89a encoder for exporting animated blink comparators.
 * Pure TypeScript, zero external dependencies.
 */

export interface GifFrame {
  canvas: HTMLCanvasElement
  delayMs: number
}

// LZW compression for GIF
function lzwEncode(minCodeSize: number, data: Uint8Array): Uint8Array {
  const clearCode = 1 << minCodeSize
  const eoiCode = clearCode + 1
  let nextCode = eoiCode + 1
  let curCodeSize = minCodeSize + 1

  const out: number[] = []
  let bitBuf = 0
  let bitLen = 0

  const writeBits = (val: number, len: number) => {
    bitBuf |= val << bitLen
    bitLen += len
    while (bitLen >= 8) {
      out.push(bitBuf & 0xff)
      bitBuf >>= 8
      bitLen -= 8
    }
  }

  // Initialize dictionary: map string of byte to code
  let table = new Map<string, number>()
  const resetTable = () => {
    table.clear()
    for (let i = 0; i < clearCode; i++) {
      table.set(String.fromCharCode(i), i)
    }
    curCodeSize = minCodeSize + 1
    nextCode = eoiCode + 1
  }

  resetTable()
  writeBits(clearCode, curCodeSize)

  let prefix = ''
  for (let i = 0; i < data.length; i++) {
    const k = String.fromCharCode(data[i])
    const pk = prefix + k
    if (table.has(pk)) {
      prefix = pk
    } else {
      writeBits(table.get(prefix)!, curCodeSize)
      if (nextCode < 4096) {
        table.set(pk, nextCode++)
        if (nextCode > (1 << curCodeSize) && curCodeSize < 12) {
          curCodeSize++
        }
      } else {
        writeBits(clearCode, curCodeSize)
        resetTable()
      }
      prefix = k
    }
  }

  if (prefix !== '') {
    writeBits(table.get(prefix)!, curCodeSize)
  }
  writeBits(eoiCode, curCodeSize)

  if (bitLen > 0) {
    out.push(bitBuf & 0xff)
  }

  // Package into sub-blocks (max 255 bytes each)
  const blocks: number[] = []
  blocks.push(minCodeSize)
  for (let p = 0; p < out.length; p += 254) {
    const chunk = out.slice(p, p + 254)
    blocks.push(chunk.length)
    blocks.push(...chunk)
  }
  blocks.push(0) // Block terminator

  return new Uint8Array(blocks)
}

/**
 * Encodes multiple canvas frames into an animated GIF Blob.
 */
export function createAnimatedGif(frames: GifFrame[], width: number, height: number): Blob {
  if (frames.length === 0) throw new Error('No frames provided')

  const bytes: number[] = []

  // 1. Header & Logical Screen Descriptor
  bytes.push(0x47, 0x49, 0x46, 0x38, 0x39, 0x61) // "GIF89a"
  bytes.push(width & 0xff, (width >> 8) & 0xff)
  bytes.push(height & 0xff, (height >> 8) & 0xff)

  // Global Color Table Flag (yes, 256 colors = 8 bits)
  // 0xF7 = 1 (GCT) | 111 (8 bits) | 0 (sort) | 111 (256 entries)
  bytes.push(0xf7)
  bytes.push(0x00) // Background color index
  bytes.push(0x00) // Pixel aspect ratio

  // Build a standard 256-color palette (adaptive or 6x6x6 color cube + greyscale)
  const palette: [number, number, number][] = []
  // 6x6x6 cube (216 colors)
  for (let r = 0; r < 6; r++) {
    for (let g = 0; g < 6; g++) {
      for (let b = 0; b < 6; b++) {
        palette.push([Math.round(r * 51), Math.round(g * 51), Math.round(b * 51)])
      }
    }
  }
  // Greyscale ramp (40 shades)
  for (let i = 0; i < 40; i++) {
    const v = Math.round((i / 39) * 255)
    palette.push([v, v, v])
  }

  // Write Global Color Table (768 bytes)
  for (const [r, g, b] of palette) {
    bytes.push(r, g, b)
  }

  // 2. Netscape Application Extension for infinite looping
  bytes.push(0x21, 0xff, 0x0b) // Extension header + 11 bytes
  const netscape = 'NETSCAPE2.0'
  for (let i = 0; i < netscape.length; i++) bytes.push(netscape.charCodeAt(i))
  bytes.push(0x03, 0x01, 0x00, 0x00, 0x00) // Loop count = 0 (infinite)

  // 3. Encode each frame
  for (const frame of frames) {
    const ctx = frame.canvas.getContext('2d')
    if (!ctx) continue
    const imgData = ctx.getImageData(0, 0, width, height).data

    // Quantize pixels to palette indices
    const indexed = new Uint8Array(width * height)
    for (let i = 0; i < imgData.length; i += 4) {
      const pr = imgData[i]
      const pg = imgData[i + 1]
      const pb = imgData[i + 2]

      let bestIdx = 0
      let bestDist = Infinity
      // Fast palette lookup
      for (let j = 0; j < palette.length; j++) {
        const [cr, cg, cb] = palette[j]
        const dr = pr - cr
        const dg = pg - cg
        const db = pb - cb
        const dist = dr * dr + dg * dg + db * db
        if (dist < bestDist) {
          bestDist = dist
          bestIdx = j
          if (dist === 0) break
        }
      }
      indexed[i / 4] = bestIdx
    }

    // Graphic Control Extension (delay time in hundredths of a second)
    const delayHundredths = Math.max(1, Math.round(frame.delayMs / 10))
    bytes.push(0x21, 0xf9, 0x04)
    bytes.push(0x04) // Disposal method: restore to background
    bytes.push(delayHundredths & 0xff, (delayHundredths >> 8) & 0xff)
    bytes.push(0x00) // Transparent color index (none)
    bytes.push(0x00) // Block terminator

    // Image Descriptor
    bytes.push(0x2c) // Separator
    bytes.push(0x00, 0x00, 0x00, 0x00) // Left, Top (0, 0)
    bytes.push(width & 0xff, (width >> 8) & 0xff)
    bytes.push(height & 0xff, (height >> 8) & 0xff)
    bytes.push(0x00) // Local color table flag = 0 (use global)

    // LZW compressed image data
    const compressed = lzwEncode(8, indexed)
    for (let i = 0; i < compressed.length; i++) {
      bytes.push(compressed[i])
    }
  }

  // 4. Trailer
  bytes.push(0x3b)

  return new Blob([new Uint8Array(bytes)], { type: 'image/gif' })
}
