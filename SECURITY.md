# 安全说明

## 报告问题

请通过 GitHub 仓库的 Security Advisory 私下报告安全问题，不要在公开 Issue 中粘贴 Cookie、Token、账号资料、协议日志或可复现凭据。

报告建议包含：

- 受影响版本或提交
- 影响范围
- 最小复现步骤
- 已脱敏的请求/响应结构
- 建议修复方向

## 本地敏感数据

程序会在本地保存头条会话、应用用户、模型密钥和任务状态。默认敏感目录为 `state/`，其中的账号密文与本机 `.secret-key` 组合后可恢复会话，因此两者都需要按凭据管理。

发布或备份项目前，请运行：

```bash
python scripts/check_secrets.py
git status --ignored
```

部署建议：

- 仅绑定可信网络接口；公开部署时使用 HTTPS 和反向代理。
- HTTPS 部署启用 `dashboard.cookie_secure = true`。
- 限制 `state/`、`artifacts/` 和生成目录的文件权限。
- 定期轮换模型 API Key 和平台会话。
- 不在日志、Issue、截图或 CI 输出中记录完整凭据。

## 支持范围

安全更新优先覆盖 `main` 分支的最新版本。平台接口变化导致的兼容性问题按普通缺陷处理。
