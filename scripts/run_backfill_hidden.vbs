Option Explicit
' Launch the one-shot historical backfill (run_backfill_oneshot.ps1) with NO visible window.
' Same zero-flash rationale as run_hidden.vbs — WScript.Shell.Run(cmd, 0, False) creates the
' process hidden from the start. Fired once by the "EconGraph backfill (one-shot)" task after
' the weekly quota reset.
Dim ps1
ps1 = "C:\Users\konda\OneDrive\econgraph_repo\scripts\run_backfill_oneshot.ps1"
CreateObject("WScript.Shell").Run _
  "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """", 0, False
