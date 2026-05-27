import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

/** Tailwind-aware class concatenator. shadcn/ui's canonical cn helper. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
