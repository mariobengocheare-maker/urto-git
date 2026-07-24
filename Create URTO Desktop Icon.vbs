' One-time setup: creates a "URTO" icon on your Windows Desktop that launches
' the app with no console window, in your normal browser. Run this once after
' extracting a new copy of URTO; then just use the Desktop icon from then on.
' Safe to run again later (e.g. after moving the folder) — it just recreates
' the icon pointing at wherever this script currently lives.

Set oFSO = CreateObject("Scripting.FileSystemObject")
scriptDir = oFSO.GetParentFolderName(WScript.ScriptFullName)

Set oWS = CreateObject("WScript.Shell")
sLinkFile = oWS.SpecialFolders("Desktop") & "\URTO.lnk"
Set oLink = oWS.CreateShortcut(sLinkFile)
oLink.TargetPath = scriptDir & "\launch_desktop.pyw"
oLink.WorkingDirectory = scriptDir
oLink.IconLocation = scriptDir & "\launch_desktop.pyw, 0"
oLink.Description = "URTO - Ultimate Realtor Tool"
oLink.Save

MsgBox "Done! A ""URTO"" icon was added to your Desktop." & vbCrLf & vbCrLf & _
       "From now on, just double-click it to open URTO - no console window, " & _
       "opens right in your browser.", 64, "URTO Setup"
