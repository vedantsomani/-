// The tour remembers that it has been seen. localStorage can throw (private windows, blocked
// site data), so every access is wrapped and the app works the same without it.
const KEY = 'sx-time-machine:tour'

export function tourSeen(): boolean {
  try {
    return window.localStorage.getItem(KEY) === 'seen'
  } catch {
    return false
  }
}

export function markTourSeen(): void {
  try {
    window.localStorage.setItem(KEY, 'seen')
  } catch {
    /* storage unavailable: the tour simply may be offered again */
  }
}
