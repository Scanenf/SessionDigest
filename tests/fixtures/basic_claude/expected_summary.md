## 这一段在做什么
你在排查 shop 项目导出 CSV 时中文文件名下载乱码的问题。助手查看导出模块的 `Content-Disposition` 写法，定位到 `filename` 直接拼接中文、没有编码。随后按你指定的 RFC 5987 `filename*` 方案，`C:/work/shop/src/export.py` 被编辑。之后执行导出测试，输出 `5 passed in 1.21s`。

## 走过的路
卡点在导出模块把 `filename` 直接拼成中文字符串，没有做编码。你决定采用 RFC 5987 的 `filename*` 编码来避免下载乱码。助手修改 `C:/work/shop/src/export.py` 后运行 `python -m pytest tests/test_export.py -q`，输出 `5 passed in 1.21s`。

## 关键标识符
- 路径：`C:/work/shop/src/export.py`
- 命令：`python -m pytest tests/test_export.py -q`
- 参数/flag：`Content-Disposition`、`filename`、`filename*`、`-q`
- 报错原文：无
- 版本/模型：无
- URL：无

## 还没完的事
- 大文件内存占用还没测；你要求先记着。
