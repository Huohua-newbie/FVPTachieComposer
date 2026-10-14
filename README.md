# FVP Tachie Composer

FVP 引擎角色立绘查看与合成工具：浏览 `.bin` / HZC 资源里的 角色 → 服装 → 动作，
点击表情差分即时合成，导出 PNG。**解压即用**，无需安装 Python。

## 下载

到 [Releases 页面](https://github.com/Huohua-newbie/FVPTachieComposer/releases/latest)
下载 `FVPTachieComposer-<版本>-win64-flet.zip`。

发布页会附带 `SHA256SUMS.txt`，解压前可对一下哈希：

```powershell
(Get-FileHash .\<下载的文件名>.zip -Algorithm SHA256).Hash.ToLower()
```

## 使用方法

1. **解压整个目录**（产物是 `exe` + 数据目录，不是单个文件），双击 `FVPTachieComposer.exe`
2. 载入资源，任选一种：
   - 把 `.bin` 或 HZC 文件夹**直接拖进程序窗口**
   - 点左侧「角色库」标题栏的 **打开 BIN** / **打开 HZC 文件夹** 图标（或按 `Ctrl+O`）
   - 把文件拖到 `FVPTachieComposer.exe` 图标上，启动即载入
3. 左侧按 **角色 → 服装 → 动作** 三级展开浏览，每级都有缩略图
4. 点击动作自动加载底图：滚轮缩放、拖拽平移，多帧底图用 `←` `→` 翻帧
5. 点击差分缩略图即时合成，右侧实时预览
6. **保存当前** 导出这一张 PNG，**批量导出** 把该底图的全部差分帧铺平
7. 换资源时点标题栏的 **载出**（`Ctrl+W`）回到初始空状态，不用重开程序

> 这张立绘没有表情差分时（例如载入的是非立绘文件夹，或该动作压根没有 `_表情` 变体），
> 右侧会直接显示当前底图帧，**保存 / 批量导出导的就是这张底图**，文件名用 `..._base_帧号.png`。
> 也就是说：看到的就能导出来，不会只让你看。

### 快捷键

| 键位 | 作用 |
| --- | --- |
| `←` `→` | 上一帧 / 下一帧 |
| `0` | 复位缩放 |
| `Ctrl+O` | 打开 BIN |
| `Ctrl+S` | 保存当前（合成图或底图） |
| `Ctrl+W` | 载出，回到初始状态 |
| `L` | 切换深浅主题 |
| `Esc` | 关闭对话框 |

## 功能亮点

- **拖进窗口即载入**：文件一进窗口就有反应——左侧「角色库」面板的边框亮起、一圈光斑沿着边跑，
  告诉你松手就会载入；拖的是不支持的类型时给一次红框提示，不会静默吞掉
- **三级目录**：角色 → 服装 → 动作，动作层再往右退一级并挂导轨线，层级一眼可读
- **语义动作名**：自动从 `CHR_角色_喜_制服` 里取出「喜」当动作名
- **即时合成**：点击差分即时预览合成结果，选中行与差分框带 180ms 过渡
- **无差分也能导出**：见上面那条提示
- **换主题不丢现场**：展开的目录、选中的动作与差分帧、底图和合成预览、标题栏的文件名全部保留，只换配色
- **导入 / 导出各记各的目录**：选完导出目录后，再开 bin 不会被带去导出那边
- **克制的界面**：浅灰页底 + 白色细描边卡片，不用纹理、渐变和阴影——立绘本身是画面里唯一该抢眼的东西
- **透明棋盘舞台**：立绘的空隙不再与纯白混淆，预览溢出被裁在自己的圆角框内，不会糊到相邻区块
- **滚动条留槽**：列表为滚动条单独留了道槽，滑块贴在槽里，不再压着缩略图和按钮

## 系统要求

- Windows 10 / 11 64-bit
- 无需安装 Python 或其他任何依赖

## 已知问题

- 拖动窗口宽度时，底部的差分条会随宽度重新排版并短暂闪烁
- 窗口窄于约 1280px 时，左侧角色树会被挤压

## 更新日志

见 [GitHub Releases](https://github.com/Huohua-newbie/FVPTachieComposer/releases)。

---

## 给开发者

- 入口：`FVPTachieComposerFlet.py`（UI）+ `FVPTachieComposer.py`（HZC / BIN 解析与合成）
- 开发运行：`py -3.13 FVPTachieComposerFlet.py <资源路径>`，原因见 [`tests/DEV.md`](tests/DEV.md)
- 构建发布：见 [`BUILD.md`](BUILD.md)
- 自检：`python tests/test_ui_smoke.py`、`python tests/test_thumb_threading.py`、
  `python .github/selfcheck.py`；人工冒烟清单见 [`TESTING.md`](TESTING.md)
