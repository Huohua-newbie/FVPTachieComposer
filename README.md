# FVP Tachie Composer

FVP 引擎角色立绘查看与合成工具，基于 Flet (Material Design 3) 构建。

## 功能

- 打开 `.bin` 文件，按 **角色 → 服装 → 动作** 三级树浏览所有立绘
- 服装/动作/角色均显示缩略图预览
- 点击动作自动加载底图，底部展示所有差分表情帧
- 点击差分缩略图即时合成预览
- 保存单张合成图 / 批量导出所有差分帧
- 深色/浅色主题切换

## 环境

- Python 3.10+
- Flet >= 0.86.5
- Pillow >= 10.3.0

## 安装

```bash
pip install -r requirements.txt
```

## 运行

```bash
python FVPTachieComposerFlet.py
```

## 项目结构

| 文件 | 说明 |
|------|------|
| `FVPTachieComposerFlet.py` | Flet UI 主程序 |
| `FVPTachieComposer.py` | BIN 文件解析与立绘合成逻辑 |
| `requirements.txt` | 依赖 |
