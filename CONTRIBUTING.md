# 贡献指南

感谢参与改进 Toutiao Auto Publisher。提交代码前请先阅读本指南。

## 开发环境

1. Fork 仓库并创建功能分支：`git checkout -b feat/short-description`。
2. 使用 Python 3.11 或更高版本创建虚拟环境。
3. 安装依赖：`pip install -r requirements.txt pytest`。
4. 安装 Node.js 依赖：`npm ci`。
5. 复制 `config.example.toml` 为 `config.toml`，仅在本地填写配置。

## 提交前检查

```bash
python scripts/check_secrets.py
python -m pytest tests -q
python -m py_compile dashboard.py toutiao_challenges.py toutiao_publisher.py
npm test
```

请勿提交以下内容：

- Cookie、CSRF Token、API Key、账号资料或登录二维码
- `state/` 下的数据库、密钥和浏览器配置
- `config.toml`、`.env`、协议日志和生成内容
- 本地字体、模型缓存、视频、音频或临时文件

## Pull Request

- 一个 PR 聚焦一个问题。
- 描述行为变化、验证方式和兼容性影响。
- 涉及协议字段时补充脱敏后的请求/响应结构和对应测试。
- 涉及界面时附桌面端与移动端截图。
- 新增配置项时同步更新 `config.example.toml` 和 README。

## 代码风格

- Python 使用类型标注，保持函数职责单一。
- JavaScript 延续现有原生 DOM/API 模式。
- 用户可见文本默认使用简体中文。
- 日志和异常不得包含 Cookie、Token、API Key 或完整响应凭据。
