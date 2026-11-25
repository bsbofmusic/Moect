"use client"

import { AnimatePresence, motion, useReducedMotion } from "framer-motion"
import { Check } from "lucide-react"

interface SelectionCheckboxProps {
  checked: boolean
  label: string
  disabled?: boolean
  title?: string
  showLabel?: boolean
  onCheckedChange: (checked: boolean) => void
}

export function SelectionCheckbox({ checked, label, disabled, title, showLabel, onCheckedChange }: SelectionCheckboxProps) {
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      title={title}
      className={showLabel ? "selection-checkbox selection-checkbox-labeled" : "selection-checkbox"}
      onClick={(event) => {
        event.stopPropagation()
        onCheckedChange(!checked)
      }}
    >
      <span className="selection-checkbox-mark" aria-hidden="true">
        <Check size={14} strokeWidth={2.5} />
      </span>
      {showLabel && <span>{label}</span>}
    </button>
  )
}

export function BatchSelectionCheckbox({ visible, ...props }: SelectionCheckboxProps & { visible: boolean }) {
  const reducedMotion = useReducedMotion()
  return (
    <AnimatePresence initial={false}>
      {visible && (
        <motion.div
          initial={{ width: 0, opacity: 0 }}
          animate={{ width: 36, opacity: 1 }}
          exit={{ width: 0, opacity: 0 }}
          transition={{ duration: reducedMotion ? 0 : 0.18, ease: "easeOut" }}
          className="shrink-0 overflow-hidden"
        >
          <SelectionCheckbox {...props} />
        </motion.div>
      )}
    </AnimatePresence>
  )
}
