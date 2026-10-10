# One-shot historical backfill — armed to run ONCE after the weekly Claude quota resets.
# Populates the longer signal-reliability horizons (+20d/+60d) from OLD news whose horizons are
# ALREADY mature at run time, so the one-shot captures each event's FULL horizon panel now.
#
# Why this window: +60d needs ~84 calendar days to mature, so only events older than that have a
# mature +60d today. The default window (May 20 – Jul 18) targets exactly those, so every event's
# full panel is computed in this run and the later prune of the (now-aged) events loses nothing —
# signal_outcomes, once written, is never pruned. (Newer events' long horizons are handled by the
# ongoing path: the cycle now retains events 95 days via PRUNE_OLDER_THAN_DAYS, long enough for
# +60d to mature, and the daily validation captures them as they do.)
#
# Pauses the hourly "So What V2 cycle" for the duration so a mid-run prune can't delete these
# old events before they're traced + validated, and so it doesn't contend for quota. The cycle is
# ALWAYS re-enabled in finally, even if the backfill errors. Tunables via env: BACKFILL_START /
# BACKFILL_END / BACKFILL_MAX_EVENTS. This is the only quota-consuming piece of the system.
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
$start = if ($env:BACKFILL_START) { $env:BACKFILL_START } else { '2026-05-20' }
$end   = if ($env:BACKFILL_END)   { $env:BACKFILL_END }   else { '2026-07-18' }
# Only honor a POSITIVE-integer override; '0'/'' /garbage fall back to 150 (PowerShell treats the
# non-empty string '0' as truthy, so a bare `if ($env:...)` would pass --max-events 0 = no cap).
$maxEvents = '150'
if ($env:BACKFILL_MAX_EVENTS -match '^[1-9]\d*$') { $maxEvents = $env:BACKFILL_MAX_EVENTS }
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
