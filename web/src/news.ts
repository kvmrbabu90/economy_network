// Morning-brief headlines: typed fetch wrapper over GET /news/headlines.

import { API_BASE_URL } from "./config";

export interface Headline {
  text: string;    // ≤15-word trimmed headline
  source: string;  // e.g. "Reuters Business"
  url: string;     // original article URL
}

/**
 * Fetch today's headlines from the API.
 *
 * The first call after a server restart triggers RSS fetching + Claude CLI
 * filtering, which can take 30-60 s. We use a 60 s timeout (not 15 s) and
 * automatically retry up to MAX_RETRIES times with exponential backoff so
 * the morning brief eventually populates without any user action.
 */
export async function fetchHeadlines(force = false): Promise<Headline[]> {
  const url = `${API_BASE_URL}/news/headlines${force ? "?force=true" : ""}`;
  const MAX_RETRIES = 3;
  const RETRY_DELAYS_MS = [8_000, 16_000, 32_000]; // backoff between attempts

  let lastErr: unknown;
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    if (attempt > 0) {
      await new Promise(r => setTimeout(r, RETRY_DELAYS_MS[attempt - 1] ?? 30_000));
    }
    const controller = new AbortController();
    // 60 s gives the Claude CLI subprocess enough time to finish on a cold cache.
    const timeoutId = setTimeout(() => controller.abort(), 60_000);
    try {
      const resp = await fetch(url, { signal: controller.signal });
      if (!resp.ok) throw new Error(`/news/headlines ${resp.status}`);
      let data: { headlines?: Headline[] };
      try {
        data = await resp.json() as { headlines?: Headline[] };
      } catch {
        throw new Error("/news/headlines: invalid JSON response");
      }
      if (!data || !Array.isArray(data.headlines)) return [];
      return data.headlines;
    } catch (err) {
      lastErr = err;
      // Check by name rather than `instanceof DOMException` — cross-realm fetches
      // (iframes, Workers) and polyfills (node-fetch, undici pre-v5) throw a
      // plain Error with name "AbortError", not a DOMException, causing the
      // instanceof check to return false and skipping all retries.
      const isAbort = err != null && (err as { name?: string }).name === "AbortError";
      if (isAbort && attempt < MAX_RETRIES) {
        // Timeout on a cold cache — server is still warming up. Retry silently.
        console.info(`news: attempt ${attempt + 1} timed out, retrying...`);
        continue;
      }
      throw err; // non-timeout error or final attempt — bubble up
    } finally {
      clearTimeout(timeoutId);
    }
  }
  throw lastErr;
}
