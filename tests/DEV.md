开发入口：直接跑脚本，不要用 `flet run -d`。

    cd D:\Download\Code\FVPTachieComposer
    py -3.13 FVPTachieComposerFlet.py "D:\soft\...\graph_bs.bin"

`flet run -d` 的热重载模式下，FilePicker 服务不会在客户端那侧建成，
所有弹框（打开 BIN / 打开文件夹 / 保存 / 批量导出）都会卡在
"Timeout waiting for invoke method listener" 上；而且重载会重跑 main()，
argv 里自动载入的资源也会丢。改完代码重启一次进程只要几秒，比对着
假死的热重载省时间。`run_hot.py` 保留着，只在不需要碰弹框的改动上用。

argv 自动载入在打包态同样要成立，但这依赖 `client\main.dart` 的 FVP-PATCH：
flet 0.86.5 原生客户端把「桌面 + 位置参数」当成 developer mode，嵌入 Python
压根不启动（窗口卡死启动屏）。改打包链时别把补丁弄丢，
`build_flet_win.ps1` 会校验并重建。
