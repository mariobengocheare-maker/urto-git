' One-time setup: creates a "URTO Updater" icon on your Windows Desktop.
' Double-click it any time to fetch the latest version of URTO, install it,
' and clean up after itself automatically. Safe to re-run this setup script
' again later (e.g. after moving the folder) — it just recreates the icon
' pointing at wherever this script currently lives.

Set oFSO = CreateObject("Scripting.FileSystemObject")
scriptDir = oFSO.GetParentFolderName(WScript.ScriptFullName)

Set oWS = CreateObject("WScript.Shell")
sLinkFile = oWS.SpecialFolders("Desktop") & "\URTO Updater.lnk"
Set oLink = oWS.CreateShortcut(sLinkFile)
oLink.TargetPath = scriptDir & "\urto_updater.pyw"
oLink.WorkingDirectory = scriptDir
oLink.IconLocation = scriptDir & "\static\urto_icon.ico, 0"
oLink.Description = "URTO Updater - fetches and installs the latest URTO"
oLink.Save

MsgBox "Done! A ""URTO Updater"" icon was added to your Desktop." & vbCrLf & vbCrLf & _
       "From now on, double-click it any time you want to update URTO - " & _
       "it downloads the latest version, installs it, stops any running " & _
       "copy for you, and cleans up after itself. No more extracting ZIPs " & _
       "by hand.", 64, "URTO Updater Setup"
