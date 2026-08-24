' Launch the worker supervisor without leaving a console window on screen.
' Windows Task Scheduler runs this file at user logon.
Dim shell, fso, baseDir, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
command = "cmd.exe /c """ & baseDir & "\start_worker.bat"""
shell.Run command, 0, False
