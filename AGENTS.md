# Project conventions

- 本项目内与用户交流和变更说明默认使用简体中文；代码标识符、型号及原文名称保持准确。
- Treat this project folder (`D:\thinksmart`) as the working root. Run project commands from here and prefer project-relative paths.
- Keep downloaded third-party tools in their own `tools/<tool-name>/` folders. scrcpy and its bundled ADB belong in `tools/scrcpy/`.
- Keep diagnostics, logs, downloads awaiting inspection, and temporary files in `work/`. Keep final deliverables in `outputs/` when needed.
- Keep tool binaries, downloaded archives, runtime data, and machine-specific shortcuts out of Git. Update `.gitignore` when adding a new tool folder.
- Track project source, launch scripts, documentation, and reusable configuration. Never commit credentials or device-private diagnostics.
- Use `Start-ThinkSmart.cmd` to open the USB control window, and `tools/scrcpy/adb.exe` for device diagnostics.
- 杜绝测试代码
- 只做最少必要文档说明，最好是不留文档
- 设备连接现状（用户确认，2026-10-31）：ThinkSmart 通电后会自动连接 Wi-Fi，无需插 USB 线；电脑可通过 `192.168.0.107` 使用 SSH 登录。
