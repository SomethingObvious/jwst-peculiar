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
