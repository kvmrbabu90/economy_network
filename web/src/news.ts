// Morning-brief headlines: typed fetch wrapper over GET /news/headlines.

export interface Headline {
  text: string;    // ≤15-word trimmed headline
  source: string;  // e.g. "Reuters Business"
  url: string;     // original article URL
}

const API_BASE = "http://localhost:8001";

export async function fetchHeadlines(force = false): Promise<Headline[]> {
  const url = `${API_BASE}/news/headlines${force ? "?force=true" : ""}`;
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`/news/headlines ${resp.status}`);
  const data = await resp.json() as { headlines: Headline[] };
  return data.headlines ?? [];
}
