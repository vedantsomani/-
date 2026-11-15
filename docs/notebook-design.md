# GAMAS observatory notebook redesign

Visual direction: warm paper, forest/rust accents, editorial serif headings and monospaced coordinates. The dark comparator is a separate image-inspection surface. Design tokens live in frontend/src/styles/tokens.css and responsive rules in frontend/src/index.css.

## Structure and data

Observe, Evidence, Spectrum, Prediction and Sky context are focused notebook sections. Sections and image settings survive shared URLs and reloads. Mobile image controls expand above the comparator. Playback pauses outside Observe. Commands, provenance and the full observation timeline remain accessible through disclosures.

The landing contact sheet uses real SPHEREx stacks; dates, wavelengths and marker positions come from returned data and supplied catalogue motion. Loading and unavailable states are explicit. No generated observations, fake metrics or simulated API responses are used. Reference example coordinates and scientific/design constants remain configuration.

Removed unused synthetic hero, decorative starfield, duplicated detector strip and unofficial logo component. Retained source investigation, catalogue layers, six image modes, difference controls, image export, field votes, spectra, held-out prediction, batch triage and archive updates. Export colours and typography match the notebook.

## Verification

- Production build and lint pass; one existing useField warning remains.
- 246 offline backend tests and both live archive/name-resolution tests pass.
- Browser checks: actual landing images and season changes; notebook sections and URL reload; image comparison; Visits and difference controls; source selection; field votes; real spectrum and prediction scrubber; PNG export; complete guided tour; mobile controls; commands; AllWISE toggle; external sky context.
- No page errors in those checks. No overflow at 1440px and 390px.
- Automated WCAG A/AA checks of landing, Observe and Evidence: zero violations. This is not a complete manual accessibility audit.
- Existing frontend component/logic checks pass for URLs, commands/reset, six image sequences and independent AllWISE matches.

Reproducible browser checks: frontend/scripts/verify-notebook*.cjs and verify-sky-context.cjs. They expect the frontend on localhost:5181, backend on port 8011 and a separately installed Playwright package (PLAYWRIGHT_MODULE can supply its absolute module path). Artifacts use temporary directories and tests use isolated browser sessions. No application dependency was added.

Pre-redesign source backup: ../space-app-backups/before-design-20261115-193532.zip. Backend source is byte-for-byte unchanged from that snapshot.
