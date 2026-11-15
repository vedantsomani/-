# Verification ? 15 November 2026

Checked the local Space App after adding visit stacks, difference display controls, and AllWISE.

## Confirmed

- Backend: 246 offline tests passed; five network-marked tests excluded from that run.
- Frontend: production build succeeds. Lint succeeds with two existing warnings in useField and useImageCache.
- Real cached WISE 0855 field: 258 exposures, 186 total stacks including 126 visit stacks, and 48 differences. Cached data rebuilt successfully under analysis version 2.7.
- HTTP checks returned valid PNGs for single exposure, season, visit, RGB, and difference images. Changing floor, gamma, and local noise changed the difference rendering.
- Quick spectrum returns 254 photometry points. Explanations and investigation reports (HTML and JSON) return HTTP 200.
- Fingerprint and holdout requests reach complete status for the cached target; live reference-catalogue availability was limited by network restrictions.
- Frontend server proxy reaches the backend.
- Server-rendered component and logic checks cover all six mode sequences, new controls, URL round trips (including source selection), visits/AllWISE commands and reset, and AllWISE match badges when Gaia is unavailable. These are not browser interaction tests.

## Fixed during verification

- AllWISE motion-fit coordinates now use the corresponding reference epoch, MJD 55400. Stationary fits retain their mean observation epoch; unavailable or unreliable motion is not extrapolated. The query explicitly requests required columns and invalidates older catalogue caches.
- AllWISE badges are rendered independently of Gaia availability. An AllWISE lookup failure no longer permits the candidate-new-object flag.
- Added regression checks for parsing/epochs, propagation, catalogue failure/retry, independent catalogue matches, and difference API parameters.

The epoch correction follows the [IRSA AllWISE column descriptions](https://irsa.ipac.caltech.edu/data/WISE/docs/release/AllWISE/expsup/sec2_1a.html); the column selection follows the [IRSA Gator API](https://irsa.ipac.caltech.edu/applications/Gator/GatorAid/irsa/catsearch.html).

## Not verified

- Fresh IRSA downloads, live AllWISE queries, and CDS name resolution: this execution environment denies outbound sockets (WinError 10013 / ConnectError). The two explicitly run live tests fail at connection establishment. This does not establish that the services fail on the user's machine.
- Full browser clicks, playback, canvas appearance, and mobile layout: the desktop automation helper is unavailable and a headless browser could not start here. Automated component rendering and logic checks passed instead.
- Optional AI explanation service was not invoked.

## Reproduce frontend component/logic checks

Start the backend on port 8011, then run `node scripts/verify-viewer.mjs` from `frontend/`. The script uses the cached WISE 0855 field and does not substitute synthetic observations for the real field; only the Gaia-outage badge case uses a synthetic catalogue response.
