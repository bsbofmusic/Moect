"use client"

import { ChevronDown, Check } from "lucide-react"
import { cn } from "@/lib/utils"
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "./dropdown-menu"

export function PillDropdown<T extends string>({
  value,
  onChange,
  options,
  leadingAction,
  disabled,
  ariaLabel,
  menuClassName,
  align = "end",
  frosted = false,
}: {
  value: T
  onChange: (v: T) => void
  options: { value: T; label: string }[]
  leadingAction?: { label: string; disabled?: boolean; onSelect: () => void }
  disabled?: boolean
  ariaLabel?: string
  menuClassName?: string
  align?: "start" | "center" | "end"
  frosted?: boolean
}) {
  const current = options.find((o) => o.value === value)
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          aria-label={ariaLabel && current ? `${ariaLabel}，${current.label}` : ariaLabel}
          className="pill-trigger"
        >
          <span className="whitespace-nowrap">{current?.label ?? ariaLabel}</span>
          <ChevronDown className="pill-trigger-chevron w-3.5 h-3.5" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align={align}
        frosted={frosted}
        className={cn("min-w-[140px] max-h-[var(--radix-dropdown-menu-content-available-height)] overflow-x-hidden overflow-y-auto", menuClassName)}
      >
        {leadingAction && <>
          <DropdownMenuItem disabled={leadingAction.disabled} onSelect={leadingAction.onSelect}>
            {leadingAction.label}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
        </>}
        {options.map((o) => (
          <DropdownMenuItem
            key={o.value}
            onSelect={() => onChange(o.value)}
            className="flex items-center justify-between"
          >
            <span>{o.label}</span>
            {o.value === value && <Check className="w-3.5 h-3.5" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
