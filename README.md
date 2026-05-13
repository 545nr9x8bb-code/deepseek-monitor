# DeepSeek Monitor

DeepSeek API 桌面用量监控面板。实时查看余额、模型费用、每日消费走势。

## 快速开始

下载 [DeepSeek Monitor.exe](https://github.com/545nr9x8bb-code/deepseek-monitor/releases/latest)，双击运行。

> 仅支持 Windows 10/11（内置 WebView2，无需额外安装）。

## 首次配置

1. 打开后输入 **DeepSeek API Key**（`sk-` 开头），保存进入主界面
2. 点击齿轮 ⚙ → 粘贴 **platformToken** → 保存，即可拉取实时用量数据

> platformToken 获取：打开 platform.deepseek.com → F12 → Application → Local Storage → 复制 `userToken`

## 功能

| 区域 | 说明 |
|------|------|
| 今日用量 | 当日 API 费用 + Top 模型占比进度条 |
| 总余额 | 账户余额 |
| 每月用量 | 柱状图，可切换月份，悬停看金额 |
| 模型用量 | 今日/本月切换，费用明细（输入/输出/缓存） |
| 本月消费 | 月度累计费用 |

## 操作

- **拖拽移动**：抓标题栏
- **缩放**：右下角拖拽
- **自动隐藏**：拖到右边缘或鼠标离开窗口 0.5 秒
- **恢复**：鼠标碰右边缘彩色标签
- **双击标题栏**：全屏/恢复
- **关闭**：最小化到托盘或退出
- **刷新**：每 3 分钟自动刷新

## 从源码运行

```
pip install -r requirements.txt
python widget.py
```
