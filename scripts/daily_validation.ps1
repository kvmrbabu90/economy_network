# Daily signal-reliability validation: prices -> signal_outcomes -> signal_reliability.
# Uses NO Claude CLI / LLM, so it never touches the weekly quota (pipeline/run_validation.py).
# Fired once a day by the "EconGraph daily validation" scheduled task via run_validation_hidden.vbs
# (zero-flash). Idempotent + incremental: only newly-mature outcomes are computed, so a fixed local
# fire time is safe regardless of the exact US market-close / EOD-data-settle timing — anything a
# given run misses is picked up by the next day's run.
$ErrorActionPreference = 'SilentlyContinue'
$repo = 'C:\Users\konda\OneDrive\econgraph_repo'
# Same shared, un-redirected DB + GKG paths the cycle task and keepalive use (NOT OneDrive, NOT
# %LOCALAPPDATA%); see scripts/ensure_servers.ps1 for why.
$env:ECONGRAPH_DB = 'C:\Users\Public\econgraph\econgraph.db'
$env:GKG_CACHE_DIR = 'C:\Users\Public\econgraph\gkg_cache'
$env:PYTHONIOENCODING = 'utf-8'   # keep the appended python output single-byte, not wide-char
Set-Location $repo
$log = "$repo\data\validation.log"
$start = Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'
"[$start] daily validation start" | Out-File -FilePath $log -Append -Encoding utf8
python -B -m pipeline.run_validation --db $env:ECONGRAPH_DB *>> $log
$done = Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'
"[$done] daily validation done (exit $LASTEXITCODE)" | Out-File -FilePath $log -Append -Encoding utf8
