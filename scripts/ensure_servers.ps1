# Keep the EconGraph backend (:8101) + frontend (:5180) HEALTHY. Idempotent and
# self-healing: restarts a server that is DOWN *or* returning errors — e.g. a stale
# process left holding the port but pointing at a moved DB (returns HTTP 500), which
# a plain "is the port listening?" check would miss. Registered to run every few
# minutes so the app survives sleep / wake / reboot / crash. Backend binds --host ::
# (the browser resolves localhost -> ::1). A retry guards against killing a server
# that's merely busy for a moment.
$ErrorActionPreference = 'SilentlyContinue'
$repo = 'C:\Users\konda\OneDrive\econgraph_repo'
# The DB lives OUTSIDE OneDrive (WAL corrupts under sync); every process must agree.
$env:ECONGRAPH_DB = 'C:\Users\konda\AppData\Local\econgraph\econgraph.db'

function Test-Url([string]$url) {
    try { return (Invoke-WebRequest $url -TimeoutSec 6 -UseBasicParsing).StatusCode -eq 200 }
    catch { return $false }
}
function Stop-Port([int]$port) {
    (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue).OwningProcess |
        Select-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
}

# Backend healthy == /health returns 200 on the exact address the browser uses (::1).
# Re-test after a short pause before acting, so a momentary blip doesn't cause a kill.
if (-not (Test-Url 'http://[::1]:8101/health')) {
    Start-Sleep -Seconds 3
    if (-not (Test-Url 'http://[::1]:8101/health')) {
        Stop-Port 8101   # clear a down OR wedged/500 process before restarting
        Start-Sleep -Seconds 1
        Start-Process -FilePath 'python' `
            -ArgumentList '-B', '-m', 'uvicorn', 'api.main:app', '--host', '::', '--port', '8101' `
            -WorkingDirectory $repo -WindowStyle Hidden
    }
}

# Frontend healthy == the Vite dev server serves 200 on /.
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
