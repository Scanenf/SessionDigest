---
title: "2026-11-03 库存系统"
date: 2026-11-03
project: 库存系统
platform: claude-code
session: "1a2b3c4d-0000-0000-0000-000000000000"
turns: 46
tags: [自动摘要]
---

## 这一段在做什么
你在给 Flask 库存系统加 Excel 导出功能，导出成功后中文文件名在浏览器里显示成乱码。你怀疑是文件编码问题，让我排查并修掉。

## 走过的路
先试了 `openpyxl` 的默认保存方式，无效；又检查了响应体的 `Content-Type`，也正常。最后定位到 `Content-Disposition` 只写了 `filename=`，浏览器按 latin-1 解析。改成同时提供 `filename*=UTF-8''` 后正常。测试过程中还发现大文件会一次性读进内存，但这次没改。

## 关键标识符
- 路径：`app/export.py`、`tests/test_export.py`
- 命令：`python -m pytest tests/test_export.py -q`、`python -m flask run`
- 参数/flag：`filename*=UTF-8''`、`as_attachment=True`
- 报错原文：`UnicodeEncodeError: 'latin-1' codec can't encode characters in position 18-20`
- 版本/模型：openpyxl 3.1.2、Flask 3.0.3、Python 3.12
- URL：无

## 还没完的事
- 大文件导出仍是一次性读入内存，超过 50MB 可能要改流式写入
- 前端下载按钮没有 loading 状态
