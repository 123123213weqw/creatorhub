# CreatorHub CLI 0.12

现有 CreatorHub 的无界面 JSON 客户端：只使用原 Hub 的业务数据库、任务队列、审批、节流和回执，不新建另一套中控台。需要 Python 3.11+，运行时无额外 Python 依赖。

## 基本用法

在本目录运行：

```sh
bin/hubctl --version
bin/hubctl schema
bin/hubctl capabilities
bin/hubctl doctor
bin/hubctl accounts list --input '{"platform":"douyin","page_size":20}'
bin/hubctl wecom moments-list --input '{}'
```

默认连接 `http://127.0.0.1:18080`。可通过权限 600 的本机配置文件提供唯一 `hub_url` 字段；只接受显式回环 HTTP 地址。团队凭据从进程环境获取，不写进命令参数、示例或仓库。模型不得选择配置/地址或清除凭据绕过角色边界。

当前包含 83 个固定命令，具体参数以 `schema` 为准：账号与快照、素材、受控同步、发布草稿与状态、内容/计划/分发、离线线索/CRM、备份、企微只读摘要和客户朋友圈任务。注册素材须由本机用户明确指定已授权的文件，模型工具只接受登记后的素材编号。

## 受控确认

新增的 `publish review/approve`、`wecom moments-review/moments-dispatch/moments-scope` 只适配现有 Hub 的固定路由。完整审核快照仅供本机人工确认器，不得直通模型。

- `publish approve` 需要 `acknowledge_external_writes=true`、有效任务 ID 和匹配的 `review_fingerprint`；仅批准进入原 Hub 发布队列，不声称已经发表。
- `wecom moments-dispatch` 对同一个正式草稿进行人工确认后的审核与提交，绑定正文/素材、执行成员及客户标签范围。仍需成员在手机企微点发表。
- Hub 继续核验版本、摘要、素材完整性、演示标记、权限与节流；CLI 不跳过这些检查。
- 写请求需要稳定 `request_key`，不自动重试。未知结果先核对原任务/回执，不重新编号或重复提交。
- 不提供任意接口、Shell、账号登录、授权变更、角色提升、自动派发或删除命令。

## JSONL / 其他 AI 接入

```sh
bin/hubctl-jsonl < examples/readonly-requests.jsonl
```

每行是 `{"id":"request-1","command":"accounts.list","args":{}}`，每行返回一份 JSON 回执。该入口默认只读，只能由本机启动者选择性开放部分本地准备命令。真实外发、批准及完整人工审核快照仍被拒绝，即使传入 allowlist 也不能开放。详见 [AI_USAGE.md](AI_USAGE.md)。

飞书的逐单确认工作流见 [`integrations/feishu-bot`](../../integrations/feishu-bot/README.md)。模型先生成本地确认单，真实用户发送同私聊精确确认码后，由本机处理器执行固定 CLI 命令。

## 与飞书确认器配合安装

在仓库根目录执行。已有同名目录/启动器时应先备份核对，不直接覆盖：

```sh
release="$HOME/.local/share/creatorhub-cli/releases/0.12.0"
mkdir -p "$(dirname "$release")" "$HOME/.local/bin"
test ! -e "$release" && cp -R clients/creatorhub-cli "$release"
test ! -e "$HOME/.local/bin/hubctl" && ln -s "$release/bin/hubctl" "$HOME/.local/bin/hubctl"
"$HOME/.local/bin/hubctl" --version
```

如不使用受控确认器，也可直接执行本目录的 `bin/hubctl`，或在 venv 内使用 `pip install .`。机器人只接受受管理发布目录里的启动器；不能把信任范围扩大到整个主目录。

## 测试

在本目录执行：

```sh
python3 -m unittest discover -s tests -q
```

测试使用合成响应、模拟传输与临时文件，不需要真实账号或平台凭据。
