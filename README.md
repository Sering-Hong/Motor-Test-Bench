# 电机测试台 MotorBench

Windows x64 上位机，用 STM32F407 C 板或 STM32H723 DM-MC02 测试 CAN 电机。支持 J-Link 和 CMSIS-DAP，可在软件内烧录测试固件并调整 SWD 烧录、读取时钟及采样频率。

## 安装

在 [GitHub Releases](https://github.com/RP27-EC/Motor-Test-Bench/releases) 下载 `MotorBench-Setup-Windows-x64.exe`。双击安装包，选好安装路径后点击“下一步”。安装结束会创建桌面快捷方式“MotorBench 电机测试台”，也可以从 Windows 开始菜单打开或从系统设置卸载。适用于 Windows 10/11 x64。

## 使用

1. 连接探针和测试板，选择板型与探针，点击“连接”。首次使用或固件版本不匹配时，点击“烧录测试固件”。
2. 打开 RM、达妙或领控页面，选择型号和 CAN 口后扫描。可一次扫描多台电机并查看收发 ID，每次选中一台测试。
3. 设定目标及限幅再启动。软件 PID 可分别调速度环和位置环的 Kp、Ki、Kd，达妙和领控也可选驱动器内置速度或位置控制。右侧显示速度、位置和电流或转矩曲线，可导出 CSV。

上方引导条按“连接探针 → 测试固件 → 扫描 → 选中 → 运行”提示当前步骤，大按钮一键完成；`Esc` 随时停止输出。运行中可在线改 PID、限幅和目标斜坡；“阶跃整定”自动做阶跃并给出上升时间、超调、稳态误差和调参方向。界面支持背景图与透明度调节（按浅色/深色分别记住），各型号参数自动记忆，运行日志在 `%LOCALAPPDATA%\MotorBench\logs`。

**实车调参模式**：不烧录、不复位，用 SWD 附加到正在运行的整车程序，读取 Keil AXF / GCC ELF 的调试信息按变量名监视（结构体、数组、指针可展开），右键 PID 结构体一键建调参回路，在线改参数（读回校验、可撤销），主动或被动（遥控给的阶跃）自动评估并记录每组参数的效果。

各型号的参数入口、零点设置和保护选项见[使用说明](使用说明.md)。

## 从源码构建

`src/` 是 Python 上位机，`firmware/` 是两块板的 STM32 固件，`probe_bridge/` 是 Rust 探针后端。`bundle/` 提供可直接烧录的测试固件。要重编固件，请先把 Arm GNU Toolchain、CMake 和 Ninja 加入 `PATH`，再运行 `python tools/build_firmware.py`，即可生成 HEX、ELF 和上位机使用的地址清单。

安装 Python 3.10 x64、支持 Rust 2024 的 Rust 工具链和 Inno Setup 6.5 或更新版，然后在 PowerShell 运行以下命令。`-IsccPath` 接受 `ISCC.exe` 的实际路径，也可以把它的目录加入 `PATH` 后省略该参数。

```powershell
python -m pip install -r requirements.txt PyInstaller
cargo build --release --locked --manifest-path probe_bridge/Cargo.toml
Copy-Item probe_bridge/target/release/motor-bench-probe.exe bundle/
.\tools\build_installer.ps1 -IsccPath 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
```

安装包输出到 `dist/MotorBench-Setup-Windows-x64.exe`。

## 授权

本项目的原创代码按 [GPLv3](LICENSE) 发布。随程序使用的第三方组件沿用各自的授权，源码及声明见 [THIRD_PARTY.md](THIRD_PARTY.md)。
