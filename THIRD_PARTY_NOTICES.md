# 第三方组件声明

本仓库包含或依赖以下第三方组件。各组件仍适用其原始许可证。

## Tesseract chi_sim traineddata

- 文件：`assets/tessdata/chi_sim.traineddata`
- 上游项目：<https://github.com/tesseract-ocr/tessdata_fast>
- 许可证：Apache License 2.0
- 用途：活动规则长图的简体中文 OCR

## 运行时依赖

Python 与 Node.js 依赖及版本范围分别记录在 `requirements.txt` 和 `package-lock.json` 中。安装依赖即表示同时接受对应上游项目的许可证。

## 字体

仓库不分发本地中文字体文件。程序优先使用用户配置字体和操作系统字体；需要自带字体时，请自行确认字体许可证并放入 `assets/fonts/`。
