# Frontend integration

Source: `Manvikamboz/GAMAS`, commit `fe0371e` (local GAMAS-latest-review checkout). Changes were selectively integrated into the existing Space App frontend. Backend source is byte-for-byte unchanged from the pre-merge backup.

## Added

- Observatory theme, landing page and example cards, detector/wavelength strip, actual observation metadata.
- Blink, side-by-side and split-wipe comparison; zoom/pan, minimap, coordinate probe using the existing exact inverse TAN projection, ruler, display palettes and contrast, fullscreen and shortcut sheet.
- Per-field candidate reviews with local persistence and CSV export.
- Discovery card PNG and animated GIF exports with actual selected images and available measurements.
- Spectrum uncertainty/band overlays and optional audio sonification; proper-motion timeline scrubber.
- Explicit loading of the large sky-context component and failed-image retry controls.

## Preserved

Visits, wavelength matching, difference colour/noise controls, Gaia/CatWISE/AllWISE layers, click-to-investigate, command box, incremental archive updates, spectrum exports, held-out prediction, reports and batch workflows remain in the existing application.

## Integration corrections

- Excluded generated offline-demo datasets and coordinate-triggered API substitution. Example buttons open real archive observations.
- Kept RGB and significance-map colours intact. LUTs operate on a small source bitmap before zoom, avoiding incorrect clipping, double transforms, and partial high-DPI rendering.
- Preserved selected-source coordinates under pan and zoom. Epoch-dependent catalogue marks are omitted from the secondary/wipe view rather than reusing marks from the wrong epoch.
- Fixed east/west sign in coordinate probing, used actual MJD timestamps and image IDs in the metadata panel.
- Scoped candidate votes to the field key, validated saved vote data, and prevented shortcuts from triggering behind dialogs.
- Added modal focus containment and Escape close. Closed audio contexts after playback.
- Export cards do not invent validation or stationarity. Supplied catalogue motion is distinguished from measured motion. No arbitrary image-centre source marker is drawn.
- The interactive proper-motion point has no fabricated uncertainty ellipse; backend prediction ellipses are retained.

## Verification

- Production frontend build passes. Lint passes with one pre-existing warning in useField.
- 246 offline backend tests pass; backend was not modified in this integration.
- Existing frontend component/logic checks pass for URLs, commands/reset, all six image sequences, new controls and independent AllWISE matches.
- Real Chrome browser checks: desktop/mobile layout, canvas rendering at device-pixel-ratio 2, source selection, side-by-side, wipe, mode changes, export dialog, ruler, shortcut modal, persisted votes and CSV, real spectrum panel and prediction timeline. No page errors.
- Downloaded PNG decodes at 1200 x 630; downloaded GIF decodes as two 360 x 360 frames; CSV includes the reviewed candidate's real coordinates and motion.

Browser checks are in `frontend/scripts/verify-browser-*.cjs`. They require a local server at localhost:5181 and a separately installed Playwright package (`PLAYWRIGHT_MODULE` may point to its absolute module directory). They use an isolated browser context and temporary artifact directory. Run component checks with `node scripts/verify-viewer.mjs` and the backend on port 8011.

Source backup: `../space-app-backups/before-frontend-merge-20261115-191130.zip`. It includes source/config/docs, excluding dependencies, caches, build output and Git metadata.
