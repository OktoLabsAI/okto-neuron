// Pure helpers for the paginated companion review queue. Kept free of '@/…' imports so
// `node --test tests/review-queue.test.mjs` can load it directly.

/** Page size the review view asks for. The badge never pages: it asks for limit=0. */
export const REVIEW_PAGE_SIZE = 50

/** Query string for GET /review-queue. limit=0 returns no items, only `total`. */
export function reviewQueueQuery(opts: { limit: number; cursor?: string | null }): string {
  const sp = new URLSearchParams()
  sp.set('limit', String(opts.limit))
  if (opts.cursor) sp.set('cursor', opts.cursor)
  return `?${sp.toString()}`
}

/** Append a page to the loaded items, skipping ids already shown (a cursor page can overlap a
 *  concurrent change); items without an id are always kept. */
export function appendPage<T extends { candidate_id?: string; id?: string }>(
  loaded: T[],
  page: T[],
): T[] {
  const seen = new Set(loaded.map((it) => String(it.candidate_id ?? it.id ?? '')).filter(Boolean))
  const fresh = page.filter((it) => {
    const id = String(it.candidate_id ?? it.id ?? '')
    return !id || !seen.has(id)
  })
  return fresh.length ? [...loaded, ...fresh] : loaded
}
