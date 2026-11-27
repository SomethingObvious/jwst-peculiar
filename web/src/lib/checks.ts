import type { Filters, VerdictRecord } from './types';

/** A rank typed into Go to Rank, or null when it isn't a whole number. */
export function parseRank(text: string): number | null {
  const t = text.trim();
  return /^\d+$/u.test(t) ? Number(t) : null;
}

/**
 * The run folder ?run= names, with a trailing slash, or null when it is on another
 * site. Runs come from this site or from a server on this computer, as a page on
 * another site could hand the viewer files made to mislead.
 */
export function runBase(raw: string | null, here: string): string | null {
  const b = raw ?? './run/';
  let u: URL;
  try {
    u = new URL(b, here);
  } catch {
    return null;
  }
  const local =
    (u.protocol === 'http:' || u.protocol === 'https:') &&
    (u.hostname === 'localhost' || u.hostname === '127.0.0.1');
  if (u.origin !== new URL(here).origin && !local) {
    return null;
  }
  return b.endsWith('/') ? b : `${b}/`;
}

/** Whether a tile passes the verdict filter and `q`, found in its key, mosaic or note. */
export function passesReview(
  f: Filters,
  q: string,
  text: string,
  v: VerdictRecord | undefined,
): boolean {
  const kept =
    f.verdict === 'all' ||
    (f.verdict === 'unreviewed' && !v?.verdict) ||
    (f.verdict === 'starred' && Boolean(v?.star)) ||
    v?.verdict === f.verdict;
  return kept && (!q || text.includes(q) || (v?.note ?? '').toLowerCase().includes(q));
}

/** Sliders and checkboxes keep their own arrow and delete keys but pass on the rest. */
export function focusKind(el: HTMLElement): { typing: boolean; widget: boolean } {
  const type = el instanceof HTMLInputElement ? el.type : '';
  const widget = type === 'range' || type === 'checkbox';
  const typing =
    el.isContentEditable ||
    el instanceof HTMLTextAreaElement ||
    el instanceof HTMLSelectElement ||
    (el instanceof HTMLInputElement && !widget);
  return { typing, widget };
}
