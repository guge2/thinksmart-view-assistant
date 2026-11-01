# Local tools

## scrcpy

- Location: `tools/scrcpy/`
- Version: `5.0.1`, Windows x64, including bundled ADB
- Official project: https://github.com/Genymobile/scrcpy
- Official archive: https://github.com/Genymobile/scrcpy/releases/download/v5.0.1/scrcpy-win64-v5.0.1.zip
- Archive SHA-256: `b12a2c4ee8be317422451fc7dcf8ee20a71b5ea7ef9ad73ddd825a25316227a5`

To restore the tool after cloning the project, download the official archive, verify its SHA-256, and extract the contents of its inner folder into `tools/scrcpy/`. `tools/scrcpy/scrcpy.exe` and `tools/scrcpy/adb.exe` should exist.

Connect the ThinkSmart over USB and double-click `Start-ThinkSmart.cmd` in the project root. Accept USB debugging on the device if prompted. The launcher selects the single connected USB Android device; disconnect other Android devices if more than one is connected.

For read-only diagnostics from PowerShell in the project root:

```powershell
.\tools\scrcpy\adb.exe devices -l
```

Tool binaries are intentionally ignored by Git. Keep scripts and documentation tracked, and add a matching `.gitignore` rule for each additional downloaded tool folder.
