# SPHEREx Time Machine

**Finds a mover and tells you what it is.** Type an object name or a sky position and the tool
fetches every NASA **SPHEREx** near-infrared (0.75-5 µm) exposure of that spot, lets you blink
through the epochs, and checks whether the target moved or changed brightness. For a source it
then fits a deblended 0.75-5 µm spectrum, with the neighbouring stars fitted out, and applies
fixed rules to say what kind of object it resembles (for example a T dwarf from its methane
band). Every result is shown with the checks behind it, and the limits are stated next to it.
Built for the NASA Space Apps 2026 challenge *"Planet X and SPHEREx"*, inspired by
[WiseView](https://ascl.net/1806.004).

![WISE 0855-0714, the coldest known brown dwarf, blinking between two SPHEREx seasons](docs/media/wise0855-season-blink.gif)

*WISE 0855−0714 in SPHEREx season stacks. At 1.1-1.6 µm (left) only a background star is
visible; at 4.4-5.0 µm (right) the Y dwarf is visible as it moves west at about 8″/yr.*

The known objects used to test the tool are in the [validation summary](#validation-summary) and
in the full generated report, [docs/validation.md](docs/validation.md). Nothing here is a
discovery claim.

## What you can do

**Blink the sky at five levels of processing.** Every view is stored in the URL, so a link
reproduces it.

| | |
| --- | --- |
| **Season stacks.** SPHEREx revisits each spot in "seasons" about six months apart. All exposures of one season and one detector are combined with inverse-variance weights, which makes faint movers easier to see. Blink the seasons and anything that shifts stands out. | ![Season stacks](docs/media/app-season-stacks.png) |
| **Wavelength-matched blink.** Every exposure samples a different wavelength, so a red star can look like it "changes" if two epochs differ in colour. This mode blinks only stacks that cover the same wavelength bin. | ![Wavelength-matched blink](docs/media/app-wavelength-matched-wise0855.png) |
| **Difference images.** Later season minus earlier season, per wavelength bin and combined per detector. A mover leaves a bright dot next to a dark one (orange = brighter later, blue = fainter later). | ![Difference image](docs/media/app-difference-wise0855.png) |
| **Single exposures.** Every exposure one by one, with exact UTC date, detector and wavelength. This is the raw data, noisier than the stacks. | ![Single exposures](docs/media/app-exposures.png) |
| **False colour.** All six detectors of a season in one picture (blue 0.75-1.6 µm, green 1.6-3.8 µm, red 3.8-5.0 µm). Ordinary stars look blue-white; cold brown dwarfs look deep red. | ![False colour](docs/media/app-false-colour-wise0855.png) |

**Read the target verdict.** For the source at the field centre the app reports mover,
variable or no detection, with an evidence level (strong, moderate or weak) built from visible
checks: seen in two or more detectors, two or more independent season comparisons, and, for
movers, motion that agrees in direction and speed. The measured proper motion is shown beside
the catalogue value when one is known. Season pairs that share a stack are not counted as
independent. A "Show flagged pixels" overlay shows which pixels were masked or flagged.

| Barnard's Star (saturated mover) | RW Aurigae (variable) |
| --- | --- |
| ![Barnard's Star difference view](docs/media/app-barnard-mover.png) | ![RW Aurigae difference view](docs/media/app-rwaur-variable.png) |

**Get a deblended spectrum and a classification.** "Spectrum & light curve" fits the source's
PSF in every exposure together with the Gaia DR3 neighbours within 30″, so a nearby star does
not leak into the spectrum. The naive aperture spectrum is shown next to it, and the blend is
flagged. Fixed rules on spectral indices (H2O, CH4 and CO windows) then give a label such as
"T dwarf-like (methane)" or "Y dwarf-like (flux only at 4-5 um)", with the reasons, the indices
and a low/medium/high confidence that is the outcome of rules, not a probability. Sources whose
cores are saturated get no spectrum and no classification.

| WISE 0855: neighbour fitted out | ε Indi B: methane band |
| --- | --- |
| ![Deblended spectrum of WISE 0855](docs/media/app-spectrum-wise0855-deblended.png) | ![Classification and spectrum of eps Ind B](docs/media/app-classification-epsindb.png) |

**Light curves.** For each wavelength bin the panel shows the PSF flux of the source in each
season with its uncertainty and a chi-squared against constant brightness. Only same-bin
measurements are compared.

**Predict, then reveal.** The hold-out test sets one season aside, fits a straight-line motion
to the others, predicts where the source must be in the held-out season (with an error
ellipse), and only then measures it there. The prediction is compared with a no-motion
hypothesis. The held-out season's pixels are not loaded until the prediction is fixed.

![Predict, then reveal](docs/media/app-predict-reveal-wise0855.png)

**Explain this candidate.** A plain-language explanation of what supports and what weakens a
candidate. The facts layer is always on and deterministic: every number is copied from the
measurement engine and each sentence cites the detector and season pair it comes from. An
optional AI rewrite is off unless the server has `ANTHROPIC_API_KEY` set. It may only rephrase
the facts, and its answer is rejected if it contains a number that is not in them; on any
failure the facts text is shown. The AI path has not been verified with a live API call.

![Explain this candidate](docs/media/app-explain-candidate.png)

**Gaia overlay and "candidate new object" flag.** Gaia DR3 sources are drawn on the image at
the date of each frame (linear proper motion, no parallax). Each candidate is matched to the
nearest Gaia source within 6″. A candidate with no match is labelled "Candidate new object (no
Gaia match)". That label is not proof of novelty: Gaia misses very red or faint objects such as
brown dwarfs, and a match can be a chance alignment.

![Gaia overlay on Barnard's Star](docs/media/app-gaia-overlay-barnard.png)

**Batch triage.** Paste or upload a list (one target per line, name or coordinates, optional
proper motion, up to 200 rows by default). The server builds each field, measures it and ranks
the list (by methane index, then 4.5 µm signal-to-noise; click a column to re-sort) so you can
open the most interesting targets first. Fields already cached finish in seconds, new ones take minutes.

![Batch triage](docs/media/app-batch-triage.png)

**Export and share.** "Copy link" restores the exact view. The spectrum panel downloads the
whole fingerprint record as JSON (with the export time and page URL) and a CSV with one row per
exposure measurement (date, detector, wavelength, flux, error, quality, flags); the batch page
downloads the ranked table as CSV and JSON. The layout works at phone width.

## Quick start

Requirements: Node ≥ 20.11 and Python 3.11 (or [uv](https://docs.astral.sh/uv/), which will
fetch 3.11 for you).

```bash
npm run setup     # creates backend/.venv (Python 3.11) and installs backend and frontend deps
npm run dev       # API on :8000, web app on http://localhost:5173
```

Click an example on the entry screen, or open a shareable link such as
<http://localhost:5173/?ra=133.760020&dec=-7.242207&name=WISE%200855-0714&pmra=-8123.7&pmdec=673.2&pmepoch=61000&mode=diff>.
The first visit to a position downloads every SPHEREx exposure covering it from IRSA, about
250 cutouts in roughly 6 minutes for a typical spot. Frames appear as they arrive and
everything is cached in `backend/cache/`, so the next visit is instant. To pre-build the demo
fields (list in `scripts/demo-targets.txt`):

```bash
npm run prewarm
```

## How it works

```
browser ──/api/frames──▶ FastAPI ──SIA2──▶ IRSA (spherex_dr1, spherex_qr3)
                             │  ──cutouts (parallel, retried)──▶ IRSA
                             ▼
  zodi subtraction → flag handling → reproject onto a common north-up grid centred on the target
  → per-frame wavelength map → cache (.npz + JSON) → season stacks, wavelength bins, differences
                             ▼
  PNG frames + metadata ─▶ blink player      /api/fingerprint ─▶ PSF photometry, spectrum, class
```

### Data path

1. IRSA SIA2 lists the SPHEREx spectral images covering the position, in each configured
   collection (`spherex_dr1` and `spherex_qr3` by default). Duplicates are removed by
   (observation id, detector).
2. A small cutout of each image is downloaded from IRSA (retried on 503). The image arrays are
   in MJy/sr and include zodiacal light; flag, variance, zodi and wavelength maps come with them.
3. Each cutout has the zodiacal light subtracted, bad pixels interpolated for display, and is
   reprojected onto a common 6.15″/pixel TAN grid, north up, with the target on the centre
   pixel. Each pixel keeps its own wavelength from the WAVE-TAB WCS.
4. Observations cluster into two to four **seasons** about six months apart. Each exposure
   yields a pair of detectors; D1-D6 cover 0.75-5 µm in order.

Fast-moving targets resolved by name carry their proper motion. If |μ| exceeds 200 mas/yr the
field is centred on the target at the SPHEREx mid-survey epoch (MJD 61000) and a red circle
marks where the target should be in every frame.

### Science method

* **Stacks.** Exposures of one season and detector are co-added with weights 1/variance,
  excluded pixels at weight 0, and per-pixel outlier rejection (|x − median| > 5σ + 0.3|median|
  when three or more frames contribute, so bright cores survive).
* **Wavelength matching.** Exposures are grouped into bins starting from the 102 official
  channels, merged until every season has at least 3 exposures in the bin. Only stacks of the
  same bin are compared. A measurement always uses a wavelength-balanced pair of seasons; pairs
  that share no bin are reported as "not comparable".
* **Detection and classification.** Differences are normalised to a robust σ. Lobes above
  4.5σ in a PSF-matched map are measured in the stacks (windowed centroids and photometry, or a
  PSF-pattern shift fit for saturated stars). A *mover* has a consistent centroid shift of at
  least 4σ and 0.15 px, in a direction and at a speed that agree across detectors and season
  pairs. A *variable* has a flux change of at least 5σ and 15% without such a shift.
* **False-alarm rules.** Candidates are demoted to "uncertain" (weak evidence, with the reason
  shown) when they sit in the halo of a saturated star, are residuals of the target's own PSF,
  are not centred on a detected source, or do not stand out above the residuals around them.
  Candidates that survive are listed with their evidence level.
* **Fingerprint.** Forced PSF photometry in every exposure, at the proper-motion-corrected
  position, using the effective PSF stored in each cutout (an 11 × 41 zone table, 5×
  oversampled). The target and up to six Gaia DR3 neighbours (G < 21.5) are fitted
  simultaneously on a 7-pixel box with a constant background, excluding blooming-flagged pixels,
  with a 3% PSF-model error added to the variance and a 2% systematic floor on neighbour flux.
* **Classification.** Indices are ratios of weighted window means (an absorption band against a
  power-law continuum). Rules choose among: star (no molecular absorption), M dwarf / late-type
  star, L dwarf-like, T dwarf-like (methane), Y dwarf-like (flux only at 4-5 µm), galaxy/blend-like
  or unclear, and not classifiable.
* **Predict, then reveal.** The registration floor is measured from isolated Gaia stars in the
  training seasons only; the held-out season is loaded after the prediction is fixed.
* **Flags.** Dead or unreliable pixels are interpolated over for display and excluded from
  photometry. OUTLIER pixels stay visible, because a real mover can trigger that flag.

## Validation summary

Six fields: five known objects and a quiet control field, all rebuilt with the current
pipeline (processing v3, analysis v6, fingerprint v2). Generated by `python -m app.report` from
the cached data; the full report, with per-detector measurements, spectra, light curves and
every non-target candidate, is in [docs/validation.md](docs/validation.md).

| Target | Expected | Verdict | Evidence | Spectral class | Measured vs catalogue |
| --- | --- | --- | --- | --- | --- |
| WISE 0855−0714 (Y dwarf) | mover | mover | strong | Y dwarf-like (medium confidence) | μ = (−7.66, +1.19)″/yr vs (−8.12, +0.67); speed −4.9%, direction 4.1° |
| Barnard's Star (saturated) | mover | mover | strong | not classifiable | μ = (−0.88, +9.46)″/yr vs (−0.80, +10.36); speed −8.6%, direction 0.9° |
| ε Indi B (T1+T6, unresolved) | mover | mover | strong | T dwarf-like, methane (high confidence) | μ = (+3.74, −2.26)″/yr vs (+3.98, −2.47); speed −6.6%, direction 0.7° |
| RW Aurigae (T Tauri) | variable | variable | strong | not classifiable | flux ratio 0.49-0.75 in 8 of 18 pairs, 2.41-5.01 µm (D4-D6); no significant change in the others listed |
| R Draconis (Mira) | variable | variable | strong | not classifiable | flux ratio 0.44-0.78 in 14 of 18 pairs, all six detectors |
| Control field (COSMOS) | nothing | no detection at centre | weak | n/a | no source at the centre |

**False alarms.** The six fields contain 24 other candidates (not the target). After the
demotion rules, all 24 are weak evidence: 23 are labelled "uncertain" and one, in the control
field, is labelled variable. None has moderate or strong evidence. An earlier run of the same
report (analysis v5, before these rules) listed 4 strong and 2 moderate candidates and 11
labelled mover or variable.
This is six fields, not a measured false-alarm rate.

The report also lists problems its own checks found, for example that the per-detector speed of
Barnard's Star ranges over 28% of the catalogue speed, and that in WISE 0855's D1-D4 the naive
aperture measures its neighbouring star and the deblended fit finds no target flux.

## Limitations

* **Resolution and blending.** Pixels are 6.15″ and the PSF about two pixels wide. Sources
  closer than that are blended: ε Indi B is two brown dwarfs in one pixel, and WISE 0855 sits
  4-10″ from a background star that dominates its D1-D4 aperture flux. Deblending uses Gaia
  positions and a model PSF, so it cannot recover a neighbour Gaia does not list.
* **Saturated stars have no spectrum and no classification.** Barnard's Star, RW Aurigae and
  R Draconis are saturated in most exposures. Wing-only fits are biased high, so the app does
  not interpret them. Their movement or brightness change is still measured from the stacks, with
  larger and less certain errors.
* **Few epochs.** There are two to four seasons per field. That gives two or three points per
  light curve and a proper-motion baseline of about a year, not an orbit.
* **Linear motion, no parallax.** Both the motion fit and the Gaia overlay use straight-line
  proper motion. For nearby objects such as Barnard's Star and ε Indi B parallax is a
  possible contribution to the offsets from catalogue values that the validation reports; the
  tool does not separate it from other systematics.
* **Evidence levels and classification confidence are rule outcomes.** They are counts of passed
  checks and fixed thresholds, not calibrated probabilities. Six fields are too few to calibrate
  them. Index-based labels are not spectral types.
* **False alarms near bright stars are demoted, not eliminated in principle.** The rules remove
  the artefacts seen in the six validation fields. Other fields may produce other artefacts.
  Absence from Gaia is not proof of a new object.
* **Planet Nine.** A distant planet is expected to be far too faint to appear in single-season
  SPHEREx data (based on published estimates, not computed here). This tool targets brighter
  movers such as nearby brown dwarfs, and says nothing about Planet Nine.
* **Not yet deployed publicly.** The Fly.io and Vercel setup in [docs/deploy.md](docs/deploy.md)
  has not been run in production. Rate limits, concurrency limits and cache eviction have been
  tested only with fakes and unit tests, not under real load.
* **AI explanations unverified.** The optional AI rewrite has not been run against the live API.

## API

Interactive docs at <http://localhost:8000/docs>. Long jobs are non-blocking: the first call
starts the job, then poll until `status` is `complete`.

| Endpoint | Description |
| --- | --- |
| `GET /api/health` | Liveness, configured collections, cache usage, job counts. |
| `GET /api/resolve?name=` | Sesame name resolver; position plus proper motion. |
| `GET /api/frames?ra=&dec=&fov=` | Builds or returns a field. RA/Dec in degrees or sexagesimal, `fov` in arcmin, `refresh`/`retry` flags. Returns grid WCS, seasons, frames, stacks and `analysis` (wavelength bins, differences, candidates, target verdict, fast-mover tracks). |
| `DELETE /api/fields/{key}` | Says the caller no longer needs a field; work other visitors poll for keeps running. |
| `GET /api/fields/{key}/frames/{id}.png` | One rendered exposure. `stretch`, `scale`, `sig_lo`/`sig_hi` or `lo`/`hi`. |
| `GET /api/fields/{key}/stacks/{id}.png` | One season stack (`s{season}_d{detector}` or per bin). |
| `GET /api/fields/{key}/diffs/{id}.png` | Significance map of a season difference; `zmax`, `smooth`. |
| `GET /api/fields/{key}/rgb/s{season}.png` and `.json` | False-colour image of a season, and its channel definition. |
| `GET /api/fields/{key}/frames/{id}/flags.png` | Flag overlay for one exposure. |
| `GET /api/fields/{key}/stacks/{id}/coverage.png` | How many exposures contributed to each pixel of a stack. |
| `GET /api/spectrum?ra=&dec=` | Quick-look forced aperture photometry on every frame (λ, flux in µJy, error, epoch, flags). |
| `GET /api/fingerprint?ra=&dec=&fov=&pmra=&pmdec=&epoch=` | Deblended PSF photometry, spectrum, light curves and classification for one source. |
| `GET /api/holdout?ra=&dec=&fov=&pmra=&pmdec=&epoch=&holdout=` | Predict-then-reveal test; `holdout` is the season index (default: the latest). |
| `GET /api/overlay?key=` | Gaia DR3 sources on the field grid, and the Gaia match for each candidate. |
| `GET /api/explain?key=&cand=&ai=` | Plain-language explanation of the target or a candidate (`cand=target` or `c3`); `ai=1` requests the optional rewrite. |
| `POST /api/batch` | Starts a batch triage run for a target list. |
| `GET /api/batch/{id}`, `.json`, `.csv` | Progress and ranked results; `DELETE /api/batch/{id}` cancels. |

## Configuration

Everything lives in `backend/app/config.py` and can be overridden with environment variables
(prefix `SPHEREX_`) or `backend/.env`. Examples:

```bash
SPHEREX_COLLECTIONS='["spherex_dr2","spherex_dr1","spherex_qr3"]'   # list a new release first
SPHEREX_MAX_CONCURRENT_BUILDS=3        # fields downloading at once
SPHEREX_CACHE_MAX_BYTES=5368709120     # cache size before least-recently-used eviction
SPHEREX_BATCH_MAX=200                  # rows per batch request
```

Per-client rate limits (general, field builds, AI explanations) are the `SPHEREX_RATE_*`
settings; `SPHEREX_TRUST_PROXY` makes the limiter read the client address appended by your
proxy. `ANTHROPIC_API_KEY` switches on the optional AI rewrite and `SPHEREX_EXPLAIN_MODEL`
selects its model.

## Deployment

The planned setup is a Fly.io backend with a persistent volume and a Vercel frontend. Steps,
costs and troubleshooting are in [docs/deploy.md](docs/deploy.md). It has not been deployed.

## Development

```bash
npm test                                                  # offline backend tests
node scripts/py.mjs -m pytest -m network -o addopts=""    # + live IRSA / CDS checks
cd frontend && npx tsc -b && npm run lint                 # type-check and lint
npm run build                                             # type-check + production build
cd backend && PYTHONPATH=. .venv/Scripts/python -m app.report   # regenerate docs/validation.md
```

(On macOS or Linux use `.venv/bin/python`.) Open items are in
[docs/submission-roadmap.md](docs/submission-roadmap.md).

## Credits and acknowledgments

* **SPHEREx** data: NASA / JPL-Caltech, served by the NASA/IPAC Infrared Science Archive
  (IRSA) at Caltech/IPAC. This work uses data from the SPHEREx mission.
* **Gaia DR3** (ESA, Gaia Data Processing and Analysis Consortium), queried through IRSA.
* **CDS** (Strasbourg): Sesame name resolution and [Aladin Lite](https://aladin.cds.unistra.fr/)
  (GPL-3.0, bundled from npm) for sky context, with HiPS surveys from IRSA.
* **WiseView** ([ASCL 1806.004](https://ascl.net/1806.004)) as the inspiration for blinking
  epochs to find motion.
* Software: astropy, reproject, NumPy and SciPy, FastAPI, React, Vite and Tailwind CSS.

### Additional viewer controls

- **Visits** combines exposures from each spacecraft fly-over. Select a detector and blink visits to inspect changes within a season. Season analysis and false colour still use full-season stacks.
- **Difference** includes colour threshold, contrast, and local-noise scaling. These affect display only; candidate detection and evidence scores are unchanged.
- **Show AllWISE sources** adds amber squares and catalogue matches alongside Gaia and CatWISE. Catalogue failures are reported independently. A nearby AllWISE source suppresses the candidate-new-object flag; absence from these catalogues is not proof of novelty.
- These view settings are preserved in shared URLs. The command box accepts visits and allwise on/off. Existing cached fields rebuild their stacks when loaded under the new analysis version.
