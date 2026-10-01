"""开发热重载入口：在独立终端运行，改代码保存后自动重载。

    cd D:\\Download\\Code\\FVPTachieComposer
    py -3.13 run_hot.py
"""
import sys

from flet.cli import main

if __name__ == "__main__":
    sys.argv = ["flet", "run", "-d", "FVPTachieComposerFlet.py"]
    main()
