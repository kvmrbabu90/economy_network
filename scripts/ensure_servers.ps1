# Ensure the EconGraph backend (:8101) + frontend (:5180) are running.
# Idempotent — starts ONLY what's currently down, so it's safe to run repeatedly.
# Registered as a scheduled task that fires at logon and every few minutes, so the
# servers survive sleep / wake / reboot / crash (a detached process does not survive
# sleep on its own). Backend binds --host :: (dual for the browser's localhost->::1).
$ErrorActionPreference = 'SilentlyContinue'
$repo = 'C:\Users\konda\OneDrive\econgraph_repo'
# The DB lives OUTSIDE OneDrive (WAL corrupts under sync); every process must agree.
$env:ECONGRAPH_DB = 'C:\Users\konda\AppData\Local\econgraph\econgraph.db'

function Test-Listening([int]$port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
}

if (-not (Test-Listening 8101)) {
    Start-Process -FilePath 'python' `
        -ArgumentList '-B', '-m', 'uvicorn', 'api.main:app', '--host', '::', '--port', '8101' `
        -WorkingDirectory $repo -WindowStyle Hidden
}

if (-not (Test-Listening 5180)) {
    Start-Process -FilePath 'cmd' `
        -ArgumentList '/c', 'npm --prefix web run dev -- --port 5180' `
        -WorkingDirectory $repo -WindowStyle Hidden
}
