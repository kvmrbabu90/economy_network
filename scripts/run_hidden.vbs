Option Explicit
' Launch the EconGraph keepalive (ensure_servers.ps1) with NO visible window.
'
' Why this wrapper exists: the "EconGraph servers" scheduled task runs every 5 minutes as an
' INTERACTIVE task (in the user's desktop session). When Task Scheduler launches powershell.exe
' directly, Windows flashes a console (conhost) window for a fraction of a second on every fire
' — even with -WindowStyle Hidden, because the window is created first and hidden a beat later.
' WScript.Shell.Run(cmd, 0, False) creates the process with window style 0 (hidden) from the
' start, so nothing ever flashes. The keepalive behaviour is otherwise identical.
Dim ps1
ps1 = "C:\Users\konda\OneDrive\econgraph_repo\scripts\ensure_servers.ps1"
CreateObject("WScript.Shell").Run _
  "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """", 0, False
