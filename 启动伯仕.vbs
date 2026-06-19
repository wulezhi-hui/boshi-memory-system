' 启动伯仕 - 无黑窗版 🦄
' 双击即可在后台启动伯仕（通过 Hermes Gateway）
' 开机自启：放入 shell:startup 文件夹

Dim shell, fso, tempFile, healthOutput, healthExit
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' 运行自检（静默，只取退出码）
tempFile = shell.ExpandEnvironmentStrings("%TEMP%") & "\boshi_health.txt"
shell.Run "python3 """ & shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.boshi\health_check.py"" > """ & tempFile & """ 2>&1", 0, True

' 读取自检结果
If fso.FileExists(tempFile) Then
    healthOutput = fso.OpenTextFile(tempFile).ReadAll()
    healthExit = Split(healthOutput, vbCrLf)(UBound(Split(healthOutput, vbCrLf)) - 1)
    fso.DeleteFile(tempFile)
End If

' 如果有错误（❌ 标记），弹窗通知
If InStr(healthOutput, "❌") > 0 Then
    MsgBox "伯仕自检发现问题！" & vbCrLf & vbCrLf & "请手动运行「启动伯仕.bat」查看详细信息。", vbExclamation, "🦄 伯仕 - 自检失败"
    WScript.Quit 1
End If

' 启动 Gateway（后台，无窗口）
shell.Run """C:\Users\Administrator\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe"" gateway run --profile default", 0, False

Set shell = Nothing
Set fso = Nothing
