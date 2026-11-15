# SPHEREx Time Machine: submission roadmap

Reviewed 14 November 2026 against the problem statement supplied in this conversation; feature checklist updated 15 November 2026 against the current local implementation. This is a prioritized proposal; unchecked items are not completed features.

**Product promise:** Anyone can enter a sky position, watch real SPHEREx observations change, investigate a candidate, and share reproducible evidence.

**Recommended signature interaction:** Mark a possible moving source, predict its position in a reserved observation, reveal that observation, inspect the evidence, and export the investigation.

## 1. Challenge requirements and feature checklist

All 15 items from the supplied checklist are included below, alongside the existing requirement to display actual SPHEREx observations. **Done** describes implemented local functionality; public deployment, broader scientific validation, and the remaining work in later sections retain their own completion criteria.

| Challenge item | Status | Where / gap |
| --- | --- | --- |
| Public-facing web tool | 🟡 Built; deployment pending | React frontend and FastAPI backend exist. [`fly.toml`](../fly.toml) and [`frontend/vercel.json`](../frontend/vercel.json) still contain `YOUR-APP-NAME` / `YOUR-BACKEND.fly.dev` placeholders. Deploy and verify that a fresh visitor can use the core flow without an account, including refresh and direct shared links. |
| Enter RA and declination | ✅ Done | Decimal and sexagesimal parsing in [`coords.ts`](../frontend/src/lib/coords.ts) and [`coords.py`](../backend/app/coords.py), plus name search via Sesame. Retain checks for invalid inputs, negative declinations, URL restoration, and positions outside the presets. |
| Display actual SPHEREx observations | ✅ Done | IRSA discovery in [`sia.py`](../backend/app/sia.py), cutout processing, dates, flags, and caching; [`ItemDetails.tsx`](../frontend/src/components/ItemDetails.tsx) links frames to source observations, and the field status exposes coverage and failures. |
| Display a sequence like a video | ✅ Done | [`BlinkPlayer.tsx`](../frontend/src/components/BlinkPlayer.tsx), [`Timeline.tsx`](../frontend/src/components/Timeline.tsx), play/pause/step/speed, and URL state. Available frames appear progressively; wavelength-matched comparisons and consistent display scaling support interpretation. |
| Test Barnard's Star, ε Indi B, WISE 0855, RW Aur, and R Dra | ✅ Done | All five are presets in [`constants.ts`](../frontend/src/lib/constants.ts), prewarm targets in [`scripts/demo-targets.txt`](../scripts/demo-targets.txt), and cases in [`report.py`](../backend/app/report.py). Results, coverage, measurements, and limitations are recorded in [`validation.md`](validation.md). |
| Easy to update with new data releases | 🟡 Mostly | `SPHEREX_COLLECTIONS` and SIA cache TTL are configured in [`config.py`](../backend/app/config.py). Existing completed fields are reused unless refreshed; a visitor-facing check-for-new-observations control remains open in section 5E. |
| Use the 102 infrared bands creatively | ✅ Done | The 102-channel definitions in [`bands.py`](../backend/app/bands.py) feed adaptive wavelength bins, wavelength-matched blinking, the spectrum chart, and PSF fingerprints. Missing channel coverage remains explicit. |
| Coadd each of the six detectors for signal-to-noise | ✅ Done | Season × detector stacks (`s{season}_d{detector}`) in [`stacks.py`](../backend/app/stacks.py), using inverse-variance weighting and outlier rejection. Each detector is stacked separately. |
| Generate spectra on the fly | ✅ Done | `/api/spectrum` provides a quick look and `/api/fingerprint` provides the deblended spectrum; routes are in [`main.py`](../backend/app/main.py). The fingerprint follows moving targets and withholds unusable saturated-source spectra. |
| Add/subtract images from different times | ✅ Done | `stacks` combines observations within seasons; `diff` compares seasons using wavelength-matched differences in [`analysis.py`](../backend/app/analysis.py). |
| User-selectable field of view | ✅ Done | [`SearchBar.tsx`](../frontend/src/components/SearchBar.tsx) offers 2–30 arcmin; the backend maximum is 60 arcmin in [`config.py`](../backend/app/config.py). |
| Overlay Gaia or WISE | 🟡 Partial | Gaia sources are drawn on SPHEREx frames and matched to candidates by [`overlay.py`](../backend/app/overlay.py). AllWISE is available as an [`AladinPanel.tsx`](../frontend/src/components/AladinPanel.tsx) background layer; WISE source markers on the blink images and WISE associations remain open. |
| Coordinates in the URL | ✅ Done | [`viewState.ts`](../frontend/src/lib/viewState.ts) restores the target, field of view, mode, detector, wavelength bin, season pair, stretch, flags, and other view settings for reproducible links. |
| Display metadata / contamination flags | ✅ Done | [`ItemDetails.tsx`](../frontend/src/components/ItemDetails.tsx) shows per-frame metadata, dates, detector, wavelength, coverage, and target/artifact flags; the player can overlay flagged pixels and the spectrum panel reports measurement quality. |
| Use Astropy / Aladin | ✅ Done | Astropy, reproject, and PyVO are in [`backend/requirements.txt`](../backend/requirements.txt); [`AladinPanel.tsx`](../frontend/src/components/AladinPanel.tsx) supplies Aladin Lite sky context. |
| Creative extras | ✅ Done locally | Predict-then-reveal hold-out test, evidence-grounded explanations with number validation, fingerprint JSON/CSV export, and a guided tour. The deterministic explanation works locally; the optional AI rewrite has not been verified with a live API call. |

The problem statement's potential considerations are optional enhancements: release updates, six-detector combinations, spectra, image addition/subtraction, field of view, Gaia/WISE context, coordinate URLs, and flags. The linked partner resources are resources, not a requirement to integrate every mission.

## 2. Finish scientific reliability first — P0

- [x] **Use comparable wavelengths for classification as well as detection.** *(Done: `matched_pair()` / `wavelength_balanced()`; every measurement row records the bins and wavelength range it used, and pairs with no shared bin are reported as not comparable.)* `backend/app/analysis.py` detects residuals using wavelength-bin differences, but the event measurements and `_target_verdict()` compare broad season/detector stacks. Different wavelength mixtures can therefore influence classification. Measure and classify with comparable wavelength support at the candidate position; carry wavelength differences and missing coverage into the evidence. Broad stacks remain useful for exploration.
- [x] **Present the current ranking score honestly.** *(Done: the percentage is gone; candidates carry a strong/moderate/weak evidence level from explicit checks, and season pairs sharing a stack are no longer counted as independent.)* `_merge_events()` assigns confidence with a hand-written agreement/significance formula. Display this as a heuristic evidence/ranking score, or replace it with explicit checks. It is not a calibrated probability. Season pairs that share observations are not independent confirmations.
- [ ] **Validate registration and image-shape effects.** Use static field sources to check residual alignment and compare point-spread functions. Account for those effects and correlated noise when assessing motion or brightness changes. *(Partial, not ticked: the hold-out test measures a registration floor from isolated Gaia stars and the analysis adds a 0.05 px floor per epoch; fingerprints use the measured ePSF. No systematic static-star alignment or PSF-mismatch study was done, and correlated noise is not modelled.)*
- [x] **Keep no-data and low-sensitivity states distinct from non-detection.** Missing coverage cannot establish that a source vanished. *(Done: season pairs without a shared wavelength bin are reported as "not comparable", a too-shallow epoch is reported separately, the app says when a detector has no difference image, and the validation report states that missing coverage is not evidence.)*
- [x] **Validate coordinates at the observation epoch.** Catalog positions of fast movers need propagation, and the app should distinguish a user's literal coordinate from a propagated named target. *(Done: fast movers are centred at MJD 61000 and the predicted position is drawn per frame; the target bar states the epoch the field is centred on; measured motion is compared with the catalogue motion for WISE 0855, Barnard's Star and epsilon Indi B in docs/validation.md. Linear motion only, no parallax.)*
- [x] **Validate spectra before interpreting them.** The existing `/api/spectrum` uses a fixed aperture position across exposures. A moving-source spectrum needs position propagation, aperture/PSF considerations, overlapping-source handling, and uncertainty checks. Keep the current product labeled quick-look. *(Done: `/api/fingerprint` follows the propagated position, fits the PSF with Gaia neighbours simultaneously, shows naive and deblended spectra side by side, and withholds spectra of saturated sources. Checked on WISE 0855 (neighbour fitted out) and epsilon Indi B (methane band) in docs/validation.md. `/api/spectrum` remains a labelled quick look.)*

IRSA's scientific spectrophotometry uses source models and measured PSFs, can fit close sources together, and provides uncertainty and quality information. Use its products/documentation as a reference for selected validation cases. [IRSA spectrophotometry documentation](https://irsa.ipac.caltech.edu/onlinehelp/spherex/spherex/sp.html)

## 3. Complete the five-target evidence set — P0

| Target | What to investigate | Evidence to save |
| --- | --- | --- |
| Barnard's Star | Proper-motion recovery and coordinate propagation | Dated frames, measured displacement, uncertainty, comparison with a propagated catalog track |
| Epsilon Indi B | Behavior of the brown-dwarf system across epochs and wavelengths | Localized source, coverage, motion evidence where measurable, blending/resolution limitations |
| WISE 0855 | Faint moving-source recovery, especially at useful longer wavelengths | Individual exposures versus stacks, motion evidence, nearby-source contamination, wavelength coverage |
| RW Aurigae | Brightness changes at comparable wavelengths | Flux versus time with uncertainties, flags, and an explanation if change is not significant |
| R Draconis | Brightness changes at comparable wavelengths | Flux versus time with uncertainties, cadence/coverage, significant or inconclusive result |

**Status (15 Nov 2026):** all five targets and a COSMOS control field are measured and written up in `docs/validation.md`, regenerated from cached data with `python -m app.report`, including limitations (blending, saturation, unresolved epsilon Indi B). A single quiet field is a count of one, not a false-alarm rate.

These are diagnostic goals, not guaranteed detections. Do not imply the Epsilon Indi B pair is separately resolved without supporting data. A known variable may show no measurable change in the available observations. Include static, crowded, and artifact-contaminated control fields beyond the five examples.

## 4. Make the public experience fast and clear — P0

- [x] Add a simple entry screen: search, choose an example, or take a short guided tour. *(Done: entry screen with search, five examples, a 1-minute tour and a batch page link.)*
- [ ] Prewarm the five examples and clearly identify cached observations by release and observation date. *(Partial: `npm run prewarm` builds them from `scripts/demo-targets.txt` and the item details show the collection and date of each exposure; nothing is prewarmed on a public server.)*
- [ ] Prioritize a useful pair of epochs in a selected detector; progressively fetch additional observations instead of making the first useful view depend on downloading everything. *(Partial: downloads interleave seasons and detectors and frames appear as they arrive; the order does not follow the detector the visitor selected.)*
- [x] Show progress, partial failures, retry, no coverage, and insufficient-comparison states clearly. *(Done: field status panel with progress, failed-cutout count and retry; a no-coverage message; "no difference image for this detector" and "not comparable" states.)*
- [x] Make beginner controls prominent: play/pause, previous/next, speed, date, detector, and field of view. Place advanced processing settings behind an expandable area. *(Done: checked in screenshots of 15 Nov; display settings sit under "Display settings (advanced)".)*
- [ ] Verify mobile layout, keyboard navigation, focus states, readable labels, accessible contrast, and reduced-motion/manual stepping. *(Partial: phone-width layout checked at 390 px on 15 Nov; space/arrow/+/- keys and a reduced-motion hook exist. No formal focus or contrast audit, and no screen-reader test.)*
- [ ] Deploy the frontend and a backend capable of maintaining long-running image jobs and a persistent cache. Test the actual public URL, including refresh and direct shared links.
- [x] Bound job concurrency and storage usage; allow users to cancel unneeded work without cancelling a field another visitor is using. *(Done in code: at most 3 concurrent field builds, per-client rate limits, an LRU-evicting 5 GiB cache, and `DELETE /api/fields/{key}` that only drops the caller's interest. Tested with fakes and unit tests only, not under real load or on a deployed server.)*

Suggested targets to measure, not current claims: cached examples usable within three seconds on a specified test connection; new users complete the core guided flow within five minutes. Record uncached archive latency separately.

## 5. Highest-value additions — P1

### A. Click a source to open an evidence panel

**Status:** done as a verdict card and candidate list with the checks behind each evidence level, per-detector measurements per season pair, Gaia match labels and a "weak evidence" toggle. Unsupported measurements are absent.

Show the image and difference view beside position/brightness measurements, dates, wavelengths, uncertainties, quality flags, coverage, and catalog information. Link every result to its contributing observations. Use supported, inconclusive, and artifact-suspect language where appropriate.

Acceptance: every candidate can be inspected without interpreting a single opaque score; unsupported measurements remain absent rather than fabricated.

### B. Add light curves and an exploratory spectrum

**Status:** done as the "Spectrum & light curve" panel (deblended and naive spectra, per-bin season light curves, classification). It is not a time series: there are two to four seasons. Saturated sources get no spectrum.

Add a brightness-over-time chart for comparable wavelength ranges and a spectrum of available measurements. Link chart selections to images. Show missing channels and observation times; the 102 bands are not all sampled simultaneously. Add moving apertures for moving sources and disclose blending.

Acceptance: RW Aurigae and R Draconis have inspectable same-band measurements; the moving-target view follows its predicted/fitted position. Clearly separate quick-look products from more carefully validated photometry.

### C. Add an AI explanation grounded in measured evidence

**Status:** the grounded facts layer is done and always on. The optional AI rewrite (needs `ANTHROPIC_API_KEY`) is implemented with number validation but was never verified with a live API call. The natural-language display commands were not built.

Start with an Explain this candidate button using structured analysis results and relevant mission documentation. Let the assistant describe supporting evidence, limitations, and an available next check. Attach observation/calculation references to claims and validate numbers against supplied results.

Natural-language display commands can be an additional small feature. Restrict commands to known actions and serialize the resulting view into the share URL. A failed AI call should leave the scientific controls usable and show its failure honestly.

Acceptance: sample explanations accurately reflect measured values, acknowledge insufficient evidence, and never turn a candidate into a confirmed new planet. Evaluate against a fixed set of reviewed cases. A custom trained detector is a later project requiring reviewed labels and independent validation.

### D. Export an investigation

**Status:** partial. Fingerprint JSON and CSV, batch CSV and JSON, and share URLs exist. There is no human-readable report, annotation export or animation export.

Export coordinates, epochs, release/observation identifiers, settings, measurements with units and uncertainties, flags, annotations, and an animation or image comparison. Provide JSON/CSV plus a human-readable report. Include contributor attribution and a share URL.

Acceptance: another visitor can reopen the same configuration; the exported record identifies the exact source data and processing version.

### E. Add epoch-aware catalog overlays and refresh controls

**Status:** partial. The Gaia DR3 overlay is epoch-aware (linear proper motion) and candidates carry Gaia matches or a "candidate new object" flag. WISE associations and a check-for-new-observations control were not built.

Add Gaia DR3 catalog markers and WISE associations with epochs, uncertainty, and alternative matches. The current Aladin background provides useful visual context but is not an epoch-aware catalog matching workflow. An absent match is not proof of novelty.

Expose last-checked time and a check-for-new-observations action. Existing collection configuration and SIA cache expiry are useful, but completed field manifests are currently reused unless refreshed; new archive observations will not necessarily enter an already-built field automatically.

Acceptance: refreshed fields can acquire newly available observations while old exported investigations remain reproducible.

## 6. One signature feature — P2

**Mark -> predict -> reveal -> explain -> share.**

**Status:** done in a simplified form. The "Predict, then reveal" panel fits a straight-line motion without one season and compares the prediction with the revealed season, and the explanation and share link follow. The visitor does not mark the source by hand, and there is no trajectory-following animation or shift-and-stack.

The visitor marks a source in multiple observations. Fit a simple motion hypothesis appropriate to the time span, then show its predicted location and uncertainty in a reserved observation. Reveal the reserved observation and evaluate agreement. Offer an object-following animation and, where justified, stacking along the proposed trajectory.

For a genuine reserved-data check, exclude the reserved observations from the fitting/model-selection path, not merely from the screen. Use an appropriate trajectory model; a long-baseline Solar System orbit cannot generally be represented by one straight line. A matching prediction supports a candidate, not an automatic discovery declaration.

Shift-and-stack is an established research technique. The proposed differentiator is the accessible, reproducible investigation experience. [Shift-and-stack research](https://arxiv.org/abs/2509.26279)

If time is short, finish P0 and the evidence/export workflow before attempting this full interaction.

## 7. Research extensions after the submission

- Simulated-source insertion and recovery experiments: measure sensitivity versus brightness, motion, wavelength, and crowding. Separate simulations from real candidate records.
- Reviewed candidate ranking and artifact models: evaluate on different sky fields/objects and observation periods from those used for training. Report recovery and false alarms rather than accuracy alone.
- Independent volunteer review: hide earlier votes during initial review, include a random audit sample, preserve attribution, and retrain models only after validating reviewed labels.
- Candidate watchlists: repeat the same investigation when new archive data arrive and report materially changed evidence.
- Source classification and spectral-template comparisons with uncertainty; avoid implying physical composition or temperature is certain from a noisy quick-look plot.
- Parallax/distance fitting only where cadence, baseline, observatory geometry, and astrometric accuracy support it.

## 8. Evidence judges should be able to inspect

Space Apps' published **2025** judging guidance covers impact, creativity, validity, relevance, and presentation. Use those themes to organize the submission. The current 2026 resources page could not be retrieved during this review, so this roadmap does not assert current format limits, deadlines, eligibility terms, award availability, or weights.

| Theme | Evidence from this project |
| --- | --- |
| Impact | Observed beginner task completion, loading measurements, accessible public use |
| Creativity | A demonstrated prediction/reveal investigation and a clear comparison with existing workflows |
| Validity | Five-target report, controls, uncertainty, flags, documented failure cases |
| Relevance | Actual SPHEREx data, coordinate search, chronological playback, optional spectral capabilities |
| Presentation | One understandable user journey and a reproducible result |

[Published 2025 judging guidance](https://spaceapps-v2-staging.s3.amazonaws.com/media/documents/Final_Space_Apps_2025_Judging_Guidelines_for_web.pdf). Check the current event's submission guide and local instructions before packaging the final entry, including AI disclosure and required acknowledgments.

Suggested demonstration: select WISE 0855, play actual observations, inspect its candidate evidence, show a wavelength comparison and grounded explanation, then export the investigation. Show the other four target results in the supporting evidence. Label known targets as validation examples. If a new candidate is shown, identify its preliminary status and outstanding checks.

Record a backup demonstration and test all public links from a fresh browser session. State which features work now and which are future work.

## 9. Useful reference sources

- [IRSA SPHEREx mission page](https://irsa.ipac.caltech.edu/Missions/spherex.html): archive releases, data explorer, science tools, calibration documentation, and required data acknowledgment.
- [Official SPHEREx tutorials](https://caltech-ipac.github.io/irsa-tutorials/spherex/): discovery, cutouts, PSF information, and mosaic examples.
- [IRSA spectrophotometry documentation](https://irsa.ipac.caltech.edu/onlinehelp/spherex/spherex/sp.html): modeling, uncertainty, blending, and quality outputs.
- [WiseView software record](https://ascl.net/1806.004): existing motion/variability viewer to credit and compare against.
- [SPHEREx ultracool-dwarf pipeline research](https://arxiv.org/abs/2604.22012): related moving-source spectrophotometry work; do not claim that capability alone is unprecedented.

The prior local verification in this conversation produced 48 passing offline tests and a successful production build. Those checks establish a useful software baseline; they do not establish astronomical accuracy, public deployment reliability, or completion of the five-target demonstrations.
