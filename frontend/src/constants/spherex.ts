/**
 * Static SPHEREx instrument constants and reference facts.
 *
 * Sources:
 * - SPHEREx instrument papers:
 *   - Crill et al., "SPHEREx: The Spectro-Photometer for the History of the Universe,
 *     Epoch of Reionization, and Ices Explorer", arXiv:2404.11017 (2024).
 *   - Hui et al. 2026, ApJS 284, 10.
 */

export interface SPHERExBandInfo {
  readonly id: number
  readonly name: string
  readonly minWavelengthUm: number
  readonly maxWavelengthUm: number
  readonly resolvingPower: number
  readonly color: string
  readonly pairedWithBand: number
  readonly description: string
}

/**
 * 6 H2RG detector arrays, Bands 1–6, covering 0.75–5.0 µm total;
 * 102 spectral channels (17 per band).
 * Bands 1&4, 2&5, 3&6 view the same sky simultaneously.
 */
export const SPHEREX_BANDS: readonly SPHERExBandInfo[] = [
  {
    id: 1,
    name: 'Band 1',
    minWavelengthUm: 0.75,
    maxWavelengthUm: 1.11,
    resolvingPower: 41,
    color: '#5b8cff', // Band 1 short NIR
    pairedWithBand: 4,
    description: 'Near-infrared, bluest band (0.75–1.11 µm, R ~41)',
  },
  {
    id: 2,
    name: 'Band 2',
    minWavelengthUm: 1.11,
    maxWavelengthUm: 1.64,
    resolvingPower: 41,
    color: '#3fc6e8', // Band 2
    pairedWithBand: 5,
    description: 'Near-infrared (1.11–1.64 µm, R ~41)',
  },
  {
    id: 3,
    name: 'Band 3',
    minWavelengthUm: 1.64,
    maxWavelengthUm: 2.42,
    resolvingPower: 41,
    color: '#4fe0a4', // Band 3
    pairedWithBand: 6,
    description: 'Near-infrared peak for solar-type stars (1.64–2.42 µm, R ~41)',
  },
  {
    id: 4,
    name: 'Band 4',
    minWavelengthUm: 2.42,
    maxWavelengthUm: 3.82,
    resolvingPower: 35,
    color: '#d9e94a', // Band 4
    pairedWithBand: 1,
    description: 'Mid-infrared ice and dust bands (2.42–3.82 µm, R ~35)',
  },
  {
    id: 5,
    name: 'Band 5',
    minWavelengthUm: 3.82,
    maxWavelengthUm: 4.42,
    resolvingPower: 110,
    color: '#ffae4a', // Band 5
    pairedWithBand: 2,
    description: 'Mid-infrared CO/CO2 sensitive (3.82–4.42 µm, R ~110)',
  },
  {
    id: 6,
    name: 'Band 6',
    minWavelengthUm: 4.42,
    maxWavelengthUm: 5.00,
    resolvingPower: 130,
    color: '#ff6b81', // Band 6 thermal NIR
    pairedWithBand: 3,
    description: 'Reddest band, optimal for cold brown dwarfs (4.42–5.00 µm, R ~130)',
  },
] as const

/** Nominal SPHEREx pixel scale on the sky in arcseconds. */
export const SPHEREX_PIXEL_SCALE_ARCSEC = 6.15

/** Total number of spectral channels across all detectors (17 channels per band). */
export const SPHEREX_TOTAL_CHANNELS = 102

/**
 * Caveat notice:
 * Within a single raw SPHEREx exposure, wavelength varies along the detector dispersion
 * direction (linear variable filter LVF), so a raw exposure is NOT a monochromatic image.
 */
export const SPHEREX_LVF_CAVEAT =
  'Within a single raw SPHEREx exposure, wavelength varies along the detector (linear variable filter). A raw exposure is not a monochromatic image.'

/**
 * Molecular absorption features in the near- and mid-infrared (0.75–5.0 µm).
 * Sources:
 * - Cushing et al. 2005, ApJ 623, 1115 (Infrared spectroscopy of ultracool dwarfs)
 * - Kirkpatrick et al. 2011, ApJS 197, 19 (Discovery of Y Dwarfs in WISE data)
 * - Geballe et al. 2002, ApJ 564, 466 (Spectral Classification of L and T Dwarfs)
 */
export interface MolecularFeature {
  readonly id: string
  readonly molecule: string
  readonly label: string
  readonly minWavelengthUm: number
  readonly maxWavelengthUm: number
  readonly centerWavelengthUm: number
  readonly description: string
  readonly citation: string
}

export const MOLECULAR_FEATURES: readonly MolecularFeature[] = [
  {
    id: 'h2o_1_4',
    molecule: 'H₂O',
    label: 'H₂O (1.33–1.48 µm)',
    minWavelengthUm: 1.33,
    maxWavelengthUm: 1.48,
    centerWavelengthUm: 1.40,
    description: 'Steam/water vapour absorption band typical of M, L, and T dwarfs.',
    citation: 'Cushing et al. 2005, ApJ 623, 1115',
  },
  {
    id: 'ch4_1_6',
    molecule: 'CH₄',
    label: 'CH₄ (1.62–1.74 µm)',
    minWavelengthUm: 1.62,
    maxWavelengthUm: 1.74,
    centerWavelengthUm: 1.66,
    description: 'Near-infrared methane absorption marking the transition into T dwarfs.',
    citation: 'Geballe et al. 2002, ApJ 564, 466',
  },
  {
    id: 'h2o_1_9',
    molecule: 'H₂O',
    label: 'H₂O (1.78–2.02 µm)',
    minWavelengthUm: 1.78,
    maxWavelengthUm: 2.02,
    centerWavelengthUm: 1.88,
    description: 'Deep water absorption groove between H-band and K-band transmission windows.',
    citation: 'Cushing et al. 2005, ApJ 623, 1115',
  },
  {
    id: 'ch4_2_2',
    molecule: 'CH₄',
    label: 'CH₄ (2.18–2.40 µm)',
    minWavelengthUm: 2.18,
    maxWavelengthUm: 2.40,
    centerWavelengthUm: 2.30,
    description: 'Strong overtone methane absorption in ultracool T and Y brown dwarfs.',
    citation: 'Geballe et al. 2002, ApJ 564, 466',
  },
  {
    id: 'h2o_2_7',
    molecule: 'H₂O',
    label: 'H₂O (2.50–2.90 µm)',
    minWavelengthUm: 2.50,
    maxWavelengthUm: 2.90,
    centerWavelengthUm: 2.70,
    description: 'Deep fundamental water vapour absorption band in cool atmospheres.',
    citation: 'Kirkpatrick et al. 2011, ApJS 197, 19',
  },
  {
    id: 'ch4_3_3',
    molecule: 'CH₄',
    label: 'CH₄ (3.15–3.55 µm)',
    minWavelengthUm: 3.15,
    maxWavelengthUm: 3.55,
    centerWavelengthUm: 3.30,
    description: 'Fundamental ν3 methane absorption band; heavily suppresses 3.3 µm flux in T and Y dwarfs.',
    citation: 'Kirkpatrick et al. 2011, ApJS 197, 19',
  },
  {
    id: 'co_4_7',
    molecule: 'CO',
    label: 'CO (4.50–4.90 µm)',
    minWavelengthUm: 4.50,
    maxWavelengthUm: 4.90,
    centerWavelengthUm: 4.67,
    description: 'Fundamental carbon monoxide absorption band in stellar and brown dwarf atmospheres.',
    citation: 'Cushing et al. 2005, ApJ 623, 1115',
  },
] as const
