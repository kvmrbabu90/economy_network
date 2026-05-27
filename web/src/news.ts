// Morning-brief headlines: typed fetch wrapper over GET /news/headlines.

import { API_BASE_URL } from "./config";

export interface Headline {
  text: string;    // ≤15-word trimmed headline
  source: string;  // e.g. "Reuters Business"
  url: string;     // original article URL
}

export async function fetchHeadlines(force = false): Promise<Headline[]> {
  const url = `${API_BASE_URL}/news/headlines${force ? "?force=true" : ""}`;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 15_000);
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
  } finally {
    clearTimeout(timeoutId);
  }
}
