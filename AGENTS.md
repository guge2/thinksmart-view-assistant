# Project conventions

- 与用户交流和变更说明使用简体中文；代码标识符、型号及原文名称保持原样。
- 设备通电后自动连 Wi-Fi：`ssh thinksmart`（`192.168.0.109`）。部署即在设备上运行 `device/*/install.sh`，或把改动的文件装到对应安装位置后重启服务。
- 第三方工具放 `tools/<name>/`，诊断、日志和临时文件放 `work/`，构建产物放 `outputs/`；这些都不进 Git，新增工具目录时同步更新 `.gitignore`。
- 不提交凭据、密钥和设备私有诊断数据。
- 文档只写最少必要内容，能不写就不写。
- 改了 `device/voice` 后在该目录跑 `python3 -m unittest discover -s tests`；改了技能、提示词或对话流程再跑 `tests/replay.sh`（在设备上调用真实模型，耗 token）。
