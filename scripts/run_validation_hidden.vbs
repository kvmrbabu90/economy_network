Option Explicit
' Launch the EconGraph daily reliability validation (daily_validation.ps1) with NO visible window.
'
' Same zero-flash rationale as run_hidden.vbs: when Task Scheduler launches powershell.exe directly
' Windows flashes a console (conhost) window for a fraction of a second even with -WindowStyle
' Hidden, because the window is created first and hidden a beat later. WScript.Shell.Run(cmd, 0,
' False) creates the process with window style 0 (hidden) from the start, so nothing ever flashes.
' Fired once a day by the "EconGraph daily validation" scheduled task.
Dim ps1
ps1 = "C:\Users\konda\OneDrive\econgraph_repo\scripts\daily_validation.ps1"
CreateObject("WScript.Shell").Run _
  "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """", 0, False
