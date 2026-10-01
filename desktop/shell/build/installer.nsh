; The Windows installer's own steps, around electron-builder's. Saved as UTF-8 with a byte order
; mark: the Russian text below is read as UTF-8 by NSIS only with one.

; Closing the application before files are replaced or removed. electron-builder's own check kills
; every process under the installation folder at once, which takes the launcher down before it
; can stop the agent it runs — and the agent is not under that folder, so it is left running. The
; window is asked to close first, which is the same as the operator closing it: the launcher stops
; the agent and exits. Only what is still there after that is ended the hard way.
; electron-builder declares $pid only for its own check, and its KILL_PROCESS (the fallback without
; PowerShell) names it to keep taskkill off the installer itself.
Var pid

!macro customCheckAppRunning
  System::Call 'kernel32::GetCurrentProcessId() i .r0'
  StrCpy $pid $0
  !insertmacro IS_POWERSHELL_AVAILABLE
  !insertmacro FIND_PROCESS "${APP_EXECUTABLE_FILENAME}" $R0
  ${If} $R0 == 0
    ${IfNot} ${isUpdated}
      ${IfNot} ${Cmd} `MessageBox MB_OKCANCEL|MB_ICONEXCLAMATION "$(appRunning)" /SD IDOK IDOK`
        Quit
      ${EndIf}
    ${EndIf}
    DetailPrint "$(appClosing)"
    ${If} $IsPowerShellAvailable == 0
      nsExec::Exec `"$PowerShellPath" -NoProfile -C "Get-Process -Name Daedalus -ErrorAction SilentlyContinue | ? { $$_.Path -and $$_.Path.StartsWith('$INSTDIR', 'CurrentCultureIgnoreCase') } | % { [void]$$_.CloseMainWindow() }; $$deadline = (Get-Date).AddSeconds(60); while ((Get-Date) -lt $$deadline) { if (@(Get-CimInstance Win32_Process | ? { $$_.ExecutablePath -and $$_.ExecutablePath.StartsWith('$INSTDIR', 'CurrentCultureIgnoreCase') }).Count -eq 0) { exit 0 }; Start-Sleep -Milliseconds 500 }; exit 1"`
      Pop $R0
    ${EndIf}
    !insertmacro FIND_PROCESS "${APP_EXECUTABLE_FILENAME}" $R0
    ${If} $R0 == 0
      !insertmacro KILL_PROCESS "${APP_EXECUTABLE_FILENAME}" 1
      Sleep 1500
    ${EndIf}
  ${EndIf}
!macroend

; Uninstalling: the data stays unless the operator says otherwise. The question is asked only by a
; real uninstall — not when an installer replaces this version (isUpdated), and not in a silent one,
; which keeps the data unless it was given --remove-data. The launcher does the work either way:
; it stops what is still running of the installation and removes its containers (Docker) or its
; downloaded runtime (native), and with --remove-data the data folder too. Its exit status is
; logged and not fatal: an uninstall that cannot reach Docker must still remove the program.
!macro customUnInstall
  ${IfNot} ${isUpdated}
    StrCpy $R8 "--keep-data"
    ClearErrors
    ${GetParameters} $R9
    ${GetOptions} $R9 "--remove-data" $R7
    ${IfNot} ${Errors}
      StrCpy $R8 "--remove-data"
    ${ElseIfNot} ${Silent}
      ${If} $LANGUAGE == 1049
        StrCpy $R6 "Удалить и данные Daedalus?$\r$\n$\r$\nРазговоры, настройки, ключи и рабочие папки агента лежат в$\r$\n$LOCALAPPDATA\Daedalus\data$\r$\n$\r$\nНажмите «Нет», чтобы их сохранить: если установить Daedalus снова, он продолжит с того же места."
      ${Else}
        StrCpy $R6 "Delete your Daedalus data as well?$\r$\n$\r$\nThe conversations, the settings, the keys and the agent's workspaces are in$\r$\n$LOCALAPPDATA\Daedalus\data$\r$\n$\r$\nChoose No to keep them: installing Daedalus again picks up where you left off."
      ${EndIf}
      ${If} ${Cmd} `MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "$R6" IDYES`
        StrCpy $R8 "--remove-data"
      ${EndIf}
    ${EndIf}
    ; daedalus:// is registered by the application itself when it starts (Electron does it under
    ; HKCU); it goes with the application, unless another copy has taken it over since.
    ReadRegStr $R5 HKCU "Software\Classes\daedalus\shell\open\command" ""
    ${If} $R5 == `"$INSTDIR\Daedalus.exe" "%1"`
      DeleteRegKey HKCU "Software\Classes\daedalus"
    ${EndIf}
    ${If} $R8 == "--remove-data"
      ; The window's own state (main.js puts it there): caches, where the window was, its log.
      RMDir /r "$LOCALAPPDATA\Daedalus\Shell"
    ${EndIf}
    DetailPrint "daedalus-desktop uninstall $R8"
    nsExec::ExecToLog `"$INSTDIR\daedalus-desktop.exe" uninstall $R8`
    Pop $R7
    DetailPrint "exit status $R7"
  ${EndIf}
!macroend
