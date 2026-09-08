"use client";

/**
 * One notice, in the house shape: a rule down the left, an icon, one sentence.
 *
 * Lifted out of the leads page when the other four screens needed it. Every
 * warning and every failure in this product looks like this, which is the point:
 * a rep on a call has to recognise "something is wrong" without reading first.
 */

import type { ReactNode } from "react";
import { TriangleAlert } from "lucide-react";

export interface NoticeProps {
  tone: "danger" | "warn";
  text: string;
  children?: ReactNode;
}

export function Notice({ tone, text, children }: NoticeProps) {
  const border = tone === "danger" ? "border-danger" : "border-warn";
  const icon = tone === "danger" ? "text-danger" : "text-warn";
  return (
    <div className={`flex gap-3 border-l-2 ${border} bg-surface-2 p-4`}>
      <TriangleAlert aria-hidden="true" className={`mt-0.5 h-4 w-4 shrink-0 ${icon}`} />
      <div className="flex min-w-0 flex-col gap-2">
        <p className="text-body text-muted">{text}</p>
        {children}
      </div>
    </div>
  );
}
