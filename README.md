# ThinkSmart View Assistant

把刷了 Ubuntu 的 Lenovo ThinkSmart View（CD-18781Y，msm8953）改成中文语音助手：离线唤醒、大模型对话、点歌、闹钟、天气和触屏界面。

## 目录

| 路径 | 内容 |
| --- | --- |
| `device/audio/` | 声卡驱动安装、UCM、WirePlumber、回声消除配置，AEC3 插件构建脚本 |
| `device/voice/` | 语音助手、音乐、闹钟、离线指令；`install.sh` 安装语音、界面、智能体和网关 |
| `device/agent/` | 大模型智能体（Node，esbuild 打包） |
| `device/ui/` | `/dev/fb0` 触屏界面 |
| `device/gateway/` | 本机管理网关 |
| `device/proxy/` | Mihomo 分流 |
| `device/clock/` | 无 RTC 时的时间保存与恢复 |
| `*.ps1` / `*.cmd` / `*.command` | 电脑端 SSH 登录、配置密钥、改唤醒词、同步代理、打开管理页 |

## 安装

前提：设备已按 [rickx/ubuntu-thinksmart](https://github.com/rickx/ubuntu-thinksmart) 刷好 Ubuntu，有免密 sudo 的用户 `thinksmart`（uid 1000）。

1. 电脑上运行 `device/audio/build-aec3.sh` 生成 `outputs/audio/`；把 `snd-soc-tas5782m.ko` 和原厂固件 `tas5728m_dsp_lenovo_cd-18781y.bin` 放进 `device/audio/`。
2. `cd device/agent && npm install && npm run build`
3. 把仓库复制到设备，依次运行 `sudo device/audio/install.sh`、`sudo device/voice/install.sh`。
4. 写入阿里云百炼密钥：`sudo thinksmart-configure-voice < aliyun.json`（`{"api_key", "workspace_id", "region"}`），之后删除该文件。可选的对话模型凭据用 `thinksmart-configure-chat`、`thinksmart-configure-expert`。

## 配置

- `/etc/thinksmart-voice.conf`：模型、音色、唤醒灵敏度、回复长度
- `/etc/thinksmart-ui.conf`：默认城市、天气刷新、自动调暗
- 屏幕设置自动保存；QQ 音乐在设置里扫码登录

## 免责声明

本项目与联想、腾讯、网易、阿里云无关。刷机可能使设备变砖，请先备份原厂分区。音乐版权归各平台和权利人所有，请遵守其用户协议。

## 许可证

[MIT](LICENSE)
