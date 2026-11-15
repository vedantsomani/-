// Minimal typings for the parts of Aladin Lite v3 we use.
declare module 'aladin-lite' {
  export interface AladinInstance {
    gotoRaDec(ra: number, dec: number): void
    setFoV(fovDeg: number): void
    setImageSurvey(id: string): void
    setBaseImageLayer(id: string): void
    addOverlay(overlay: unknown): void
    removeOverlay?(overlay: unknown): void
    getRaDec(): [number, number]
    on(event: string, cb: (...args: any[]) => void): void
  }
  interface GraphicOverlay {
    add(shape: unknown): void
    removeAll(): void
  }
  const A: {
    init: Promise<void>
    aladin(el: HTMLElement | string, opts?: Record<string, unknown>): AladinInstance
    graphicOverlay(opts?: Record<string, unknown>): GraphicOverlay
    polygon(points: [number, number][], opts?: Record<string, unknown>): unknown
    circle(ra: number, dec: number, radiusDeg: number, opts?: Record<string, unknown>): unknown
  }
  export default A
}
