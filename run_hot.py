"""开发热重载入口：在独立终端运行，改代码保存后自动重载。

    cd D:\\Download\\Code\\FVPTachieComposer
    py -3.13 run_hot.py [bin 或 hzc 目录]

透传的参数会作为应用自己的 sys.argv[1:]，于是启动即自动载入该资源。
"""
import sys

from flet.cli import main

if __name__ == "__main__":
    sys.argv = ["flet", "run", "-d", "FVPTachieComposerFlet.py", *sys.argv[1:]]
    main()
