开发入口：直接跑脚本，不要用 `flet run -d`。

    cd D:\Download\Code\FVPTachieComposer
    py -3.13 FVPTachieComposerFlet.py

`flet run -d` 的热重载模式下，FilePicker 服务不会在客户端那侧建成，
所有弹框（打开 BIN / 打开文件夹 / 保存 / 批量导出）都会卡在
"Timeout waiting for invoke method listener" 上。改完代码重启一次进程只要几秒，比对着
假死的热重载省时间。`run_hot.py` 保留着，只在不需要碰弹框的改动上用。

载入资源统一走界面内拖放或工具栏图标：应用不再读 argv（打包版客户端带
位置参数就不执行入口脚本，永远卡在启动屏），测试里要起干净状态后自己调
`_load_bin` / 载入路径。
