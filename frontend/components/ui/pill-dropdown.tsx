"use client"

import { ChevronDown, Check } from "lucide-react"
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
  align = "end",
}: {
  value: T
  onChange: (v: T) => void
  options: { value: T; label: string }[]
  leadingAction?: { label: string; disabled?: boolean; onSelect: () => void }
  disabled?: boolean
  align?: "start" | "center" | "end"
}) {
  const current = options.find((o) => o.value === value)
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          className="inline-flex items-center gap-1.5 h-8 pl-3 pr-2.5 text-[13px] font-medium rounded-full bg-white dark:bg-neutral-900 border border-black/[0.03] dark:border-white/10 shadow-[0_1px_2px_rgba(0,0,0,0.02)] text-neutral-700 dark:text-neutral-200 outline-none cursor-pointer hover:border-black/[0.08] hover:bg-neutral-50 dark:hover:bg-white/5 transition-all disabled:opacity-50 disabled:pointer-events-none"
        >
          <span className="whitespace-nowrap">{current?.label}</span>
          <ChevronDown className="w-3.5 h-3.5 text-neutral-400" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align={align} className="min-w-[140px]">
        {leadingAction && <>
          <DropdownMenuItem disabled={leadingAction.disabled} onSelect={leadingAction.onSelect}>
            {leadingAction.label}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
        </>}
        {options.map((o) => (
          <DropdownMenuItem
            key={o.value}
            onClick={() => onChange(o.value)}
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
