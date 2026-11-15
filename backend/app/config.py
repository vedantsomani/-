"""Runtime configuration. Every value can be overridden with an env var prefixed
``SPHEREX_`` (e.g. ``SPHEREX_COLLECTIONS='["spherex_dr2","spherex_dr1"]'``) or a
``backend/.env`` file. New SPHEREx data releases only need ``collections`` changed."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SPHEREX_", env_file=BACKEND_DIR / ".env", extra="ignore")

    # --- IRSA ---------------------------------------------------------------
    sia_url: str = "https://irsa.ipac.caltech.edu/SIA"
    # Queried in parallel. When the same exposure+detector appears in several
    # collections, the one listed FIRST wins (put the newest release first).
    collections: list[str] = ["spherex_dr1", "spherex_qr3"]
    # Radius (deg) of the SIA search circle. Tiny on purpose: we want images
    # that contain the target, not ones that merely graze the field.
    sia_radius_deg: float = 0.0005
    sia_cache_ttl_s: int = 6 * 3600

    gator_url: str = "https://irsa.ipac.caltech.edu/cgi-bin/Gator/nph-query"
    wise_table: str = "allwise_p3as_psd"
    overlay_wise_margin_arcsec: float = 140.0
    overlay_wise_max_sources: int = 400

    sesame_url: str = "https://cds.unistra.fr/cgi-bin/nph-sesame/-oxp/SNV"

    # --- downloading --------------------------------------------------------
    download_concurrency: int = 12
    download_retries: int = 4
    http_timeout_s: float = 180.0
    # Where cutouts come from, tried in order: "s3" builds them from the AWS open-data copy of
    # the L2 files with HTTP range requests (~1-2 s, see s3cutout.py); "irsa" is the IRSA cutout
    # service (10-16 s each, frequent 503s). A failure of one source falls through to the next.
    cutout_sources: list[str] = ["s3", "irsa"]
    s3_concurrency: int = 48        # concurrent S3 range requests (separate from IRSA's limit)
    s3_timeout_s: float = 60.0

    # --- processing ---------------------------------------------------------
    pixel_scale_arcsec: float = 6.15  # native SPHEREx pixel ~6.15"
    default_fov_arcmin: float = 5.0
    max_fov_arcmin: float = 60.0
    # Extra cutout margin (arcmin) so rotated detector frames fully cover the grid.
    cutout_margin_arcmin: float = 1.0
    processing_workers: int = 4
    # Background mesh cell (pixels). 16 px = 98": large vs. the ~1.5 px PSF, small
    # enough to follow scattered-light gradients across a field.
    background_box_px: int = 16

    # Flag policy (names come from the MSKN/MSKM keywords in each FLAGS header).
    # mask: pixel value is meaningless -> interpolated over for display, excluded from photometry.
    mask_flags: list[str] = [
        "OVERFLOW", "SUR_ERROR", "NONFUNC", "MISSING_DATA", "HOT", "COLD",
        "NONLINEAR", "PERSIST", "TRANSIENT",
    ]
    # suspect: shown as-is (OUTLIER can be a real mover/flare!) but excluded from photometry.
    suspect_flags: list[str] = ["OUTLIER"]
    # artifact: contamination by instrument artifacts; reported, not masked.
    artifact_flags: list[str] = [
        "CROSSTALK", "GHOST", "GHOST_FPA", "GHOST_EXT", "STREAK", "BLOOM", "SNOWBALL", "HALO",
        "PHANMISS",
    ]
    # Anything else (SOURCE, DICHROIC, FULLSAMPLE) is informational.

    # --- fingerprint (forced PSF photometry, milestone 3) --------------------
    # AWS open-data mirror of the IRSA SPHEREx tree; used to range-read the EPSF table.
    spherex_s3_base: str = "https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com/"
    tap_url: str = "https://irsa.ipac.caltech.edu/TAP"
    gaia_table: str = "gaia_dr3_source"
    catalog_timeout_s: float = 150.0      # IRSA TAP cone searches take 3-80 s
    # Gaia sources within this distance of the target's path are fitted simultaneously.
    neighbour_radius_arcsec: float = 30.0
    blend_radius_arcsec: float = 10.0       # neighbours closer than this -> blend_warning
    max_neighbours: int = 6                 # brightest first
    neighbour_max_gmag: float = 21.5        # fainter Gaia sources are not worth a parameter
    # Fit region: square box (native px, odd) around the target, grown to cover neighbours.
    fit_box_px: int = 7
    fit_background: str = "constant"        # constant | plane
    # Extra flags excluded from the PSF fit (not from display): bloomed charge around a
    # saturated core inflates the wings that a saturated-star fit depends on.
    fit_exclude_flags: list[str] = ["BLOOM"]
    # ePSF model uncertainty (fraction of the model per pixel) added to the variance.
    psf_model_error: float = 0.03
    # Naive comparison aperture (native px): radius and sky annulus.
    aperture_r_px: float = 1.5
    annulus_px: tuple[float, float] = (4.0, 7.0)
    fingerprint_workers: int = 4
    # Saturated stars: fit the amplitude of the empirical extended PSF (psf.ExtendedProfile)
    # on unmasked wing pixels out to `wing_fit_r_px`, excluding the masked core dilated by
    # `wing_rim_px` (non-linear rim). Fluxes are calibrated against unsaturated stars and
    # carry the measured systematic. Off -> saturated cores stay wings-within-ePSF / bad.
    # OFF BY DEFAULT (Nov 2026 validation): the calibration does not transfer to saturated
    # stars. Barnard's Star fluxes jump ~2x between detectors at their overlaps and change
    # 2-3x when the excluded rim grows 1 -> 3 px; unsaturated calibrators (<= 0.2 Jy) only
    # constrain the wing/core ratio to ~25-60% at the r_in ~ 5 px of real saturated cores.
    # Enable (SPHEREX_SATURATED_WINGS=1) only for relative, same-channel light curves.
    saturated_wings: bool = False
    wing_fit_r_px: float = 9.0
    wing_fit_background: str = "plane"   # other stars' halos make the sky slope over 19 px
    wing_rim_px: int = 1
    wing_model_error: float = 0.10       # extended-profile uncertainty, fraction of the model
    psf_fetch_concurrency: int = 3

    # --- storage / server ---------------------------------------------------
    cache_dir: Path = BACKEND_DIR / "cache"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # --- public-use hardening -----------------------------------------------
    # Failed exposures. Transient failures (HTTP 429/5xx, timeouts, connection errors) are
    # retried automatically when a complete field is loaded and the last attempt is older
    # than the cool-down, at most `auto_retry_max` times; `/api/frames?retry=1` always retries.
    # Permanent failures (HTTP 4xx, "no coverage") are never retried.
    retry_cooldown_s: float = 900.0
    auto_retry_max: int = 3
    # Field builds. At most `max_concurrent_builds` fields download at once; the rest are
    # `queued`. A build nobody has polled for `job_idle_cancel_s` is cancelled (finished
    # frames stay on disk, so a later request resumes). DELETE /api/fields/{key} only drops
    # the caller's interest; the build is cancelled once nobody polls for `release_grace_s`.
    max_concurrent_builds: int = 3
    job_idle_cancel_s: float = 600.0
    release_grace_s: float = 60.0
    janitor_interval_s: float = 15.0
    # Cache limit. Whole fields are evicted least-recently-used (processed products and
    # fingerprints first, raw cutouts next) down to `cache_evict_target` * limit. Fields that
    # are building or were accessed within `cache_protect_s` are never evicted; neither are
    # the ePSF libraries (cache/psf), SIA listings or Gaia catalogues.
    cache_max_bytes: int = 5 * 1024**3
    cache_protect_s: float = 600.0
    cache_evict_target: float = 0.85
    cache_check_interval_s: float = 120.0
    # Rate limits: token buckets per client IP. Rates are per minute, bursts in requests.
    # Only trust X-Forwarded-For behind a reverse proxy you control (SPHEREX_TRUST_PROXY=1);
    # the entry `trusted_proxy_hops` from the right is used (1 = the proxy's own view).
    trust_proxy: bool = False
    trusted_proxy_hops: int = 1
    rate_general_per_min: float = 1800.0   # cheap GETs and PNGs (a blink preloads hundreds)
    rate_general_burst: float = 1500.0
    rate_build_per_min: float = 6.0        # starting a NEW field build / retry
    rate_build_burst: float = 6.0
    rate_ai_per_min: float = 4.0           # /api/explain?ai=1
    rate_ai_burst: float = 6.0

    # --- catalogue overlay --------------------------------------------------
    overlay_margin_arcsec: float = 120.0   # extra Gaia search radius: high-PM stars move ~100" in 10 yr
    overlay_max_fov_arcmin: float = 30.0
    overlay_max_sources: int = 2500        # brightest first; matching still uses every source
    overlay_gaia_concurrency: int = 2
    overlay_retry_after_s: float = 300.0   # a failed Gaia query is retried after this
    overlay_match_arcsec: float = 6.0
    overlay_novelty_arcsec: float = 10.0
    overlay_hpm_masyr: float = 150.0


settings = Settings()
