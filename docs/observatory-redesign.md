# GAMAS observatory frontend

## Design

The frontend now uses a graphite observatory theme with mint and copper accents, a custom crosshair identity, a larger type scale, and shared visual tokens for the landing page and scientific workspace. Existing scientific processing and backend code are preserved.

- Homepage: real Barnard's Star preview with date selection and optional blinking; an illustrative orbital coordinate grid with CSS depth; scroll reveals; five curated target links; interactive detector-range explanation; guided and research entry points.
- Workspace: Guided / Research preference, introductory comparison actions, full instrumentation in Research, visible observation timeline, larger playback controls, consistent panel surfaces.
- Advanced wavelength and exposure links open the research controls automatically.
- Example URLs include the configured preferred detector (D6, D3, D4, D2, D3 respectively).
- Exports: unified colours and branding, manual epoch controls, pause control, reduced-motion support, and honest missing-analysis labels.
- Branding: matching favicon, header, page title, and footer.

## Motion and scientific integrity

The orbital grid and target symbols are illustrative; the archive preview uses returned SPHEREx imagery. No generated image stands in for an observation. The preview retains its square image geometry and has no sepia filter. Predicted catalogue position is identified separately from an observed detection.

Scroll reveals and orbital motion respect reduced-motion preferences. Preview blinking is opt-in, stops updating when offscreen or the tab is hidden, and supports manual date selection. Export preview motion respects reduced-motion preferences and has pause and epoch controls.

## Verification

- TypeScript and Vite production build passed.
- Frontend HTTP endpoint returned 200.
- Barnard's Star API returned 358 exposures, three seasons, and three D3 season-preview stacks.
- A D3 preview PNG returned HTTP 200 with image/png content type.
- Browser visual verification remains incomplete: the browser connector was unavailable, and the native browser window remained minimized after a restore attempt.

Pre-redesign copies of the principal changed files were saved under `C:/Users/vedan/Downloads/space-app-backups/observatory-redesign-20261115`. This preserves the existing uncommitted implementation rather than relying on the older Git baseline.
