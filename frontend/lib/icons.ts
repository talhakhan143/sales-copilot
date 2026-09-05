import type { LucideIcon } from "lucide-react";
import {
  ArrowDown,
  BadgeDollarSign,
  Ban,
  Building2,
  CalendarCheck,
  Check,
  ChevronDown,
  Clock,
  Copy,
  FileText,
  Gauge,
  Globe,
  HelpCircle,
  Mail,
  Mic,
  MicOff,
  Radio,
  RotateCcw,
  ScreenShare,
  Sparkles,
  Waves,
  X,
  Zap,
} from "lucide-react";

/**
 * Static import map. Every icon the product uses, named exactly as the backend
 * sends it in the quick action payload, plus the handful of interface icons.
 *
 * This is deliberately a static map and not a dynamic or namespace import: a
 * namespace import pulls the entire lucide set into the client bundle.
 */
export const ICONS: Record<string, LucideIcon> = {
  /* The eight frozen objection keys, backend "icon" strings. */
  BadgeDollarSign,
  Ban,
  Mail,
  Building2,
  Clock,
  HelpCircle,
  FileText,
  CalendarCheck,

  /* Interface icons. */
  ArrowDown,
  Check,
  ChevronDown,
  Copy,
  Gauge,
  Globe,
  Mic,
  MicOff,
  Radio,
  RotateCcw,
  ScreenShare,
  Sparkles,
  Waves,
  X,
  Zap,
};

/**
 * Resolve a backend icon name to a component. Unknown names fall back to Zap so
 * a new quick action from the server can never render an empty chip.
 */
export function iconFor(name: string): LucideIcon {
  const found: LucideIcon | undefined = ICONS[name];
  return found ?? Zap;
}
