"""开发热重载入口：在独立终端运行，改代码保存后自动重载。

    cd D:\\Download\\Code\\FVPTachieComposer
    py -3.13 run_hot.py

注意：`flet run -d` 下 FilePicker 不会在客户端建成，所有弹框（加载/保存/批量导出）
会 10s 超时。应用已不读 argv（打包版客户端带位置参数就不执行入口脚本），
资源只能在界面里拖入或点图标载入。要验这些路径请直接
`py -3.13 FVPTachieComposerFlet.py`，见 tests/DEV.md。
"""
import sys

from flet.cli import main

if __name__ == "__main__":
    sys.argv = ["flet", "run", "-d", "FVPTachieComposerFlet.py"]
    main()
