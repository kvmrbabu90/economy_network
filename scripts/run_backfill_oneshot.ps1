# One-shot historical backfill — armed to run ONCE after the weekly Claude quota resets.
# Populates the longer signal-reliability horizons (+20d/+60d) from ~Jul-Sep news.
#
# Pauses the hourly "So What V2 cycle" for the duration so its 30-day prune can't delete the
# old backfilled events before they're traced + validated (signal_outcomes, once written, is
# never pruned, so the reliability record survives afterward). The cycle is ALWAYS re-enabled
# in finally, even if the backfill errors. Tunables via env: BACKFILL_START / BACKFILL_END /
# BACKFILL_MAX_EVENTS. This is the only quota-consuming piece of the reliability system.
$ErrorActionPreference = 'Continue'
$repo = 'C:\Users\konda\OneDrive\econgraph_repo'
$env:ECONGRAPH_DB = 'C:\Users\Public\econgraph\econgraph.db'
$env:GKG_CACHE_DIR = 'C:\Users\Public\econgraph\gkg_cache'
$env:PYTHONIOENCODING = 'utf-8'
$env:PRUNE_OLDER_THAN_DAYS = '250'   # belt-and-suspenders if a cycle still fires somehow
Set-Location $repo
$log = "$repo\data\backfill.log"

# Window defaults chosen so horizons are MATURE by the run date: +60d needs ~Jul signals,
# +20d needs base <= ~mid-Sep. Override via env to widen/narrow.
$start = if ($env:BACKFILL_START) { $env:BACKFILL_START } else { '2026-07-01' }
$end   = if ($env:BACKFILL_END)   { $env:BACKFILL_END }   else { '2026-09-12' }
$maxEvents = if ($env:BACKFILL_MAX_EVENTS) { $env:BACKFILL_MAX_EVENTS } else { '150' }
$cycle = 'EconGraph So What V2 cycle'

("[{0}] backfill start: window {1}..{2}, max-events {3}" -f (Get-Date -Format s), $start, $end, $maxEvents) |
    Out-File $log -Append -Encoding utf8
try {
    try { Disable-ScheduledTask -TaskName $cycle -ErrorAction Stop | Out-Null } catch {}
    try { Stop-ScheduledTask    -TaskName $cycle -ErrorAction Stop } catch {}   # stop an in-flight cycle
    python -B scripts\backfill_history.py --start $start --end $end --sample-hours 24 `
        --max-events $maxEvents --trace-batch 10 --db $env:ECONGRAPH_DB *>> $log
}
finally {
    try { Enable-ScheduledTask -TaskName $cycle -ErrorAction Stop | Out-Null } catch {}
    ("[{0}] backfill done (cycle re-enabled, exit {1})" -f (Get-Date -Format s), $LASTEXITCODE) |
        Out-File $log -Append -Encoding utf8
    # One-shot: disable self so it never re-runs. Re-enable manually to run again.
    try { Disable-ScheduledTask -TaskName 'EconGraph backfill (one-shot)' -ErrorAction Stop | Out-Null } catch {}
}
