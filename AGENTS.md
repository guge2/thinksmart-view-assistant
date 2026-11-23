# Project conventions

- 与用户交流和变更说明使用简体中文；代码标识符、型号及原文名称保持原样。
- 设备通电后自动连 Wi-Fi：`ssh thinksmart`（`192.168.0.109`）。部署即在设备上运行 `device/*/install.sh`，或把改动的文件装到对应安装位置后重启服务。
- 第三方工具放 `tools/<name>/`，诊断、日志和临时文件放 `work/`，构建产物放 `outputs/`；这些都不进 Git，新增工具目录时同步更新 `.gitignore`。
- 不提交凭据、密钥和设备私有诊断数据。
- 文档只写最少必要内容，能不写就不写。
- 开发依赖装在 `tools/dev`：`uv venv --python 3.12 tools/dev && uv pip install --python tools/dev/bin/python -r requirements-dev.txt`。
- 提交前在仓库根目录跑 `./check.sh`（ruff、shellcheck、语音和网关单元测试，CI 同样执行）；改了技能、提示词或对话流程再跑 `device/voice/tests/replay.sh`（在设备上调用真实模型，耗 token）。
- `device/voice/assistant.py` 只负责组装组件和唤醒-对话主循环，各部分职责见其模块文档；新逻辑放进对应模块，不要堆回 `ThinkSmartAssistant`。
