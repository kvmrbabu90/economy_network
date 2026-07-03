# Keep the EconGraph backend (:8101) + frontend (:5180) HEALTHY and SINGLE-INSTANCE.
# Idempotent + self-healing. Runs every few minutes (Task Scheduler) so the app
# survives sleep / wake / reboot / crash. Handles three failure modes seen in the wild:
#   1. Server DOWN            -> start it.
#   2. Server WEDGED (500)    -> a stale proc holds the port but points at a moved DB;
#                               "is the port listening?" can't see this, so we check
#                               /health returns 200, and if not, kill + restart.
#   3. DUPLICATE backend proc -> a second uvicorn that lost the port-bind race keeps
#                               running as an orphan; it can grab the port on the next
#                               restart and serve 500s. Even when healthy, reap any
#                               api.main uvicorn proc that is NOT the current :8101 owner.
# Backend binds --host :: (the browser resolves localhost -> ::1). A re-test after a
# short pause guards against killing a server that is merely busy for a moment.
$ErrorActionPreference = 'SilentlyContinue'
$repo = 'C:\Users\konda\OneDrive\econgraph_repo'
# The DB lives OUTSIDE OneDrive (WAL corrupts under sync); every process must agree.
$env:ECONGRAPH_DB = 'C:\Users\konda\AppData\Local\econgraph\econgraph.db'

function Test-Url([string]$url) {
    try { return (Invoke-WebRequest $url -TimeoutSec 6 -UseBasicParsing).StatusCode -eq 200 }
    catch { return $false }
}
function Port-Owner([int]$port) {
    (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue).OwningProcess |
        Select-Object -Unique
}
function Stop-Port([int]$port) {
    Port-Owner $port | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
}
function Get-BackendProcs {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*uvicorn*api.main*' }
}
function Start-Backend {
    Start-Process -FilePath 'python' `
        -ArgumentList '-B', '-m', 'uvicorn', 'api.main:app', '--host', '::', '--port', '8101' `
        -WorkingDirectory $repo -WindowStyle Hidden
}

# --- Backend: healthy == /health returns 200 on the exact address the browser uses.
if (Test-Url 'http://[::1]:8101/health') {
    # Steady state. Reap any DUPLICATE backend proc that isn't the current port owner,
    # so orphans can't accumulate and hijack the port on a later restart.
    $owner = Port-Owner 8101
    if ($owner) {
        Get-BackendProcs | Where-Object { $_.ProcessId -ne $owner } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
}
else {
    Start-Sleep -Seconds 3
    if (-not (Test-Url 'http://[::1]:8101/health')) {
        # Down OR wedged: clear EVERY api.main uvicorn proc (listener + orphans) and the
        # port, then start exactly one clean instance.
        Get-BackendProcs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Stop-Port 8101
        Start-Sleep -Seconds 1
        Start-Backend
    }
}

# --- Frontend: healthy == the Vite dev server serves 200 on /.
if (-not (Test-Url 'http://localhost:5180/')) {
    Start-Sleep -Seconds 3
    if (-not (Test-Url 'http://localhost:5180/')) {
        Stop-Port 5180
        Start-Sleep -Seconds 1
        Start-Process -FilePath 'cmd' `
            -ArgumentList '/c', 'npm --prefix web run dev -- --port 5180' `
            -WorkingDirectory $repo -WindowStyle Hidden
    }
}
