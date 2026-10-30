# CreatorHub 上游同步 CI

仅适用于 `123123213weqw/creatorhub`，上游固定为 `3441293738/creatorhub` 的 `main`。

## 流程

1. 每小时第 17 分钟（UTC；北京时间同样是每小时第 17 分钟）检查，也可手动运行。
2. 在无写入凭据的临时 runner 中合并上游，冲突则失败，不覆盖本地定制。
3. 将确切候选提交打成 Git bundle，记录 SHA-256，并在独立的只读 runner 中跑抖音登录、接口、风控回归。
4. 仅在回归通过、`main` 未被并发更新时，以普通 fast-forward push 更新 fork 源码。写入 job 不检出或执行候选源码。

上游变更 `.github/` 时停止，需人工审核 CI；不会使用 force push，不同步标签，不触发 Windows 发布，不部署到 Mac/V100，不持有飞书、DeepSeek、抖音 Cookie 或 SSH 密钥。仅使用 GitHub 自动提供的短期 `GITHUB_TOKEN`。

## 启用与验收

- 将配置 PR 合入默认分支后，定时任务才会生效。
- 配置 PR 自身会执行 prepare 与 regression，但绝不执行 publish-source。
- 合并后可在 Actions 的 **Sync and validate CreatorHub upstream** 中点击 **Run workflow** 验证首轮发布。
- 成功同步仅代表这些离线回归通过，不代表完整测试、实际账号登录或线上部署已验收。
- runner 固定为 Ubuntu 24.04、测试 Python 固定为 3.11；所用 Actions 固定到已核对的完整提交 SHA。
- GitHub 定时任务可能延迟；公开仓库长期无活动时，平台可能自动停用定时任务。可用手动运行恢复检查。

## 失败处理

- 合并冲突或 `.github/` 更新：人工审核后同步，不自动忽略文件。
- 上游历史被重写：基于初始源提交及之后的同步合并记录检查，停止而不自动接受强推。
- 依赖安装或测试失败：保持 fork 的 `main` 不变，在对应 Actions 日志定位。
- `main` 并发更新：本轮不推送，下轮基于新 `main` 重新准备并测试。
- 网络错误：每次抓取最多两次，正常 TLS 校验，带退避和进程组硬超时；不更换上游、不使用私人镜像。

单独的 Codex 每小时同步任务已暂停；CI 不依赖 Mac 或 Codex 保持在线。
