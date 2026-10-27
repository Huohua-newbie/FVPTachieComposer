"""FVPTachieComposer 应用内模块。

拆自原先 2600 余行的单文件 `FVPTachieComposerFlet.py`，按四块划分：

- `theme.py` / `media.py` / `dropzone.py` / `drop.py`：无状态的基础设施
- `ui.py`（`UiMixin`）：布局与控件树
- `state.py`（`StateMixin`）：主题、提示条、选中与预览状态、快捷键
- `resources.py`（`ResourceMixin`）：解析、载入/载出、缩略图流水线
- `export.py`（`ExportMixin`）：保存与批量导出

`ComposerApp` 由这些 mixin 组合而成，本身只保留 `__init__` 与入口。
"""
