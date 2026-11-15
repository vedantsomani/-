// The view modes in the order the toolbar shows them, with their one-line help.
import { RGB_LEGEND } from './items'
import type { Mode } from './viewState'

export const MODE_TABS: { mode: Mode; label: string; help: string }[] = [
  {
    mode: 'stacks',
    label: 'Seasons',
    help: 'One deep image per observing season (all exposures of that season combined). Blink them and watch for anything that shifts.',
  },
  {
    mode: 'bins',
    label: 'λ-matched',
    help: 'Season images built only from one narrow wavelength bin, so a change is not just a colour difference.',
  },
  {
    mode: 'visits',
    label: 'Visits',
    help: 'Each pass of the spacecraft (a few days) combined into one image, to see changes within a season.',
  },
  {
    mode: 'diff',
    label: 'Difference',
    help: 'Later season minus earlier season at matched wavelengths. Static sources cancel; a mover leaves a bright and a dark lobe.',
  },
  {
    mode: 'frames',
    label: 'Exposures',
    help: 'Every single exposure. Noisier than stacks, but it is the raw data with exact dates.',
  },
  {
    mode: 'rgb',
    label: 'Colour',
    help: `All six detectors of a season in one false-colour image (${RGB_LEGEND}). Cold brown dwarfs look deep red.`,
  },
]
