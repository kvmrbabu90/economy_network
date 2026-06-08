# EconGraph

A single, queryable, directed graph of the global economy. Type in any news headline — watch the supply-chain impact propagate hop-by-hop through companies, commodities, and regions, with LLM reasoning at every step.

**5,334 nodes · 18,558 edges · every relationship sourced to a filing, Wikidata, or Wikipedia**

![EconGraph globe view](docs/assets/globe-preview.png)

---

## What it does

| Feature | Description |
|---|---|
| **Graph explorer** | 2D (Sigma.js) and 3D globe (Three.js) views of the full economic network |
| **"So What?" impact engine** | Paste a news headline → BFS propagation through the graph with LLM scoring at each hop |
| **Morning brief** | Daily headlines filtered for supply-chain relevance (Claude mode only) |
| **Inspector** | Click any node or edge for details, magnitude, and source provenance |
| **Search** | Find any company, commodity, or region by name or ticker |

---

## Quick start

### Prerequisites

- Python 3.11+
- Node.js 18+
- **One** of:
  - [Claude Code CLI](https://claude.ai/download) — installed and signed in to your Claude account
  - [Ollama](https://ollama.com) — running locally with a model pulled (tested: `gemma4:26b`)

### 1. Clone and install dependencies

```bash
git clone https://github.com/kvmrbabu90/economy_network.git
cd economy_network

# Python dependencies
pip install -r requirements.txt

# Frontend dependencies
cd web && npm install && cd ..
```

### 2. Download the graph database

The compiled graph is not stored in the repo. Download `econgraph.db` from the
[latest GitHub Release](https://github.com/kvmrbabu90/economy_network/releases/latest)
and place it in the **repo root**:

```
economy_network/
├── econgraph.db   ← place it here
├── api/
├── web/
└── ...
```

### 3. Configure your LLM

```bash
cp .env.example .env
```

Open `.env` and set your provider:

**Option A — Claude Code CLI** (default, nothing to change):
```env
IMPACT_LLM_PROVIDER=claude
```
Make sure `claude` is in your PATH and you're signed in (`claude --version` should work).

**Option B — Ollama / Gemma 4** (fully local, no cloud):
```bash
ollama pull gemma4:26b      # ~15 GB download, one-time
```
```env
IMPACT_LLM_PROVIDER=ollama
ECONGRAPH_LLM_MODEL=gemma4:26b
```

### 4. Start the servers

**Windows** — double-click `dev.bat`, or run in two terminals:
```bat
python -m uvicorn api.main:app --host :: --port 8101 --reload
cd web && npm run dev -- --port 5180
```

**Mac / Linux**:
```bash
chmod +x start.sh && ./start.sh
```

Open **[http://localhost:5180](http://localhost:5180)** in your browser.

---

## LLM provider comparison

| | Claude Code CLI | Ollama (Gemma 4) |
|---|---|---|
| Impact propagation | ✅ | ✅ |
| Node descriptions | ✅ | ✅ |
| Morning brief headlines | ✅ | ❌ (shows empty) |
| Cost | Claude Max plan | Free / local |
| Hardware requirement | Minimal | ~16 GB RAM for gemma4:26b |
| Quality | Higher | Good |
| Speed (full 3-hop trace) | ~2 min | Varies by hardware |

Smaller Ollama models (`gemma3:12b`, `llama3.1:8b`) also work — set `ECONGRAPH_LLM_MODEL` in `.env`.

---

## Using the impact engine

1. Open the app at [http://localhost:5180](http://localhost:5180)
2. Click the **lightning bolt** icon (or the news brief panel) to open the impact input
3. Paste or type a headline — examples:
   - *"TSMC halts advanced chip exports to China"*
   - *"Russia suspends grain exports through the Black Sea"*
   - *"Fed raises rates by 50bp"*
4. Click **So What?** and wait ~2 minutes
5. Nodes light up by direction (teal = positive, coral = negative) and hop distance
6. Click any highlighted node to see the reasoning and supply-chain path

---

## Architecture

```
economy_network/
├── api/
│   ├── main.py        ← FastAPI: /ego, /subgraph, /search, /impact, /news
│   ├── impact.py      ← BFS impact propagation engine (Claude or Ollama)
│   ├── news.py        ← Daily headline filtering
│   └── query.py       ← Graph query helpers
├── pipeline/          ← Data ingestion (SEC EDGAR, Wikidata, Wikipedia)
├── schema/
│   ├── models.py      ← Pydantic Node / Edge / Provenance types
│   └── store.py       ← SQLite schema + upsert helpers
├── web/src/           ← Vite + TypeScript frontend
│   ├── main.ts        ← App entry point
│   ├── render3d.ts    ← Three.js globe renderer
│   └── impact.ts      ← Impact overlay logic
├── config/            ← YAML: regulators, retail markets, commodity routing
├── econgraph.db       ← SQLite graph (download from Releases)
├── dev.bat            ← Windows launcher
└── start.sh           ← Mac/Linux launcher
```

### Data flow
```
SEC EDGAR / Wikidata / Wikipedia
        ↓
pipeline/ (ingest → extract → resolve → build_graph)
        ↓
econgraph.db  (SQLite: nodes, edges, aliases, provenance)
        ↓
api/main.py   (FastAPI)
        ↓
web/src/      (Sigma.js 2D + Three.js globe)
```

---

## Rebuilding the database from scratch

The pre-built `econgraph.db` covers the S&P 500 + ~2,000 global companies. To rebuild
from source (requires Claude CLI, ~4 hours, internet access to SEC EDGAR):

```bash
python -m pipeline.commodities        # commodity + region nodes
python -m pipeline.extract            # rule + LLM extraction from 10-K/20-F filings
python -m pipeline.extract_wikipedia  # Wikipedia Business/Operations edges
python -m pipeline.wikidata_phase_b   # Wikidata P1830 competitor edges
python -m pipeline.resolve            # canonicalise, de-dupe, threshold
python -m pipeline.build_graph        # load SQLite, emit graph.json
```

---

## Key invariants

1. **`customer_of` is never stored** — derived at query time by reversing `supplies`
2. **One node per entity** — "P&G as center" and "P&G inside Costco's view" are the same node
3. **Every edge has provenance** — filing accession + URL + verbatim snippet
4. **LLM edges are grounded** — the source snippet must literally contain the target entity name

---

## License

MIT
