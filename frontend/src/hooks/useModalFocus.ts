import { useEffect, type RefObject } from 'react'
export function useModalFocus(ref: RefObject<HTMLElement | null>, open: boolean, onClose: () => void) {
  useEffect(() => {
    if (!open) return
    const prior = document.activeElement as HTMLElement | null
    const root = ref.current
    const focusable = () => Array.from(root?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input, select, [tabindex="0"]') ?? [])
    focusable()[0]?.focus()
    const key = (e: KeyboardEvent) => {
      if(e.key === 'Escape') {e.preventDefault(); onClose()}
      if(e.key !== 'Tab')return
      const list=focusable(), first=list[0], last=list.at(-1)
      if(e.shiftKey && (document.activeElement===first || !root?.contains(document.activeElement))) {e.preventDefault();last?.focus()}
      else if(!e.shiftKey && (document.activeElement===last || !root?.contains(document.activeElement))) {e.preventDefault();first?.focus()}
    }
    document.addEventListener('keydown',key)
    return () => {document.removeEventListener('keydown',key);prior?.focus()}
  },[open,ref,onClose])
}
