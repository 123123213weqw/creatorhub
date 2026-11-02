# 飞书私聊 → CreatorHub CLI

飞书是自然语言入口，`hubctl` 是受控调用接口，现有 CreatorHub 是唯一业务中控台、业务数据库和队列。本目录不包含账号、Cookie、密钥、数据库、会话正文或本机部署配置。

## 能力

共 46 个工具：28 个只读查询、16 个写操作准备、2 个工具发现/账号解析辅助。首次仅发送 5 个基础工具定义，随后按业务域与 read/prepare 模式加载。

- 只读：账号、素材摘要、作品、队列、发布、企微任务、备份、CRM 预检、内容与分发任务等。
- 准备：发布草稿/批准入队、企微朋友圈草稿/成员派发/状态刷新、受控同步、内容与计划、员工分发任务、备份创建/校验、离线线索提议。
- 不提供任意 HTTP、Shell、路径、密钥、授权修改、删除、恢复覆盖或模型自行批准工具。

写工具只创建本机确认单。真实用户必须在同一个白名单私聊发送精确的 `确认 XXXXXXXX`；10 分钟过期，下一张替代上一张。“好的/继续”不执行。确认码和本机完整审核快照不进入模型上下文。执行前重新检查内容指纹，Hub 仍负责版本、素材、范围、权限与节流。

**发布入队不等于发布成功；企微派发只创建成员待发表任务，成员仍需在手机企微点发表；成员发表不等于客户看到或已读。** 普通查询读 Hub 缓存；官方刷新和平台同步需单独确认。

## 上下文与 token

保留最近三轮对话（历史总计最多 5000 字符）、最多 6 个结构化对象引用和一个待确认请求的模型参数（最多 1800 字符），仅在内存中按发送者/私聊隔离；30 分钟不活跃过期。`清空上下文` 同时取消待确认操作。多个候选对象须澄清，历史不是最新状态或授权。

同轮重复只读调用可复用，跨轮重新查询。工具结果按字段投影，每个列表最多送入模型 20 条，并标记截断。日志只记录 token usage、缓存命中/未命中、轮数、字节数和耗时等元数据；API 缺少 usage 时记为未知，而不是零。日志不记录消息正文、响应正文、推理、密钥或确认码。

## 本机安装

需要 Python 3.11+、运行于 `http://127.0.0.1:18080` 的本机 Hub，以及本仓库的 CLI 0.12+。本机可使用系统包管理器或 Python venv，不需要 Rust 编译。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
```

先按 [`clients/creatorhub-cli`](../../clients/creatorhub-cli/README.md) 安装 CLI。机器人默认从 `~/.local/bin/hubctl` 找入口，且只接受 `~/.local/share/creatorhub-cli/releases/` 下的受管理发布目录。如需自定义目录，由本机管理员设置 `HUILIMA_CLI_RELEASE_ROOT`；模型不能设置该值。不得将根目录放宽到整个主目录或文件系统。

```sh
mkdir -p ~/.config/huilima-bot
chmod 700 ~/.config/huilima-bot
cp config.example.json ~/.config/huilima-bot/config.json
chmod 600 ~/.config/huilima-bot/config.json
```

在本机私有配置中填入自己的飞书应用 ID/Secret、明确的私聊发送者白名单与 DeepSeek 凭据，再按需启用 AI 和 Hub。示例默认关闭两者。不要将真实配置放回仓库。仅使用已授权的私聊事件，不读取群聊。事件订阅与已有最小应用权限由应用管理员配置，本程序不自动申请或扩权。

```sh
.venv/bin/python bot.py
```

程序使用飞书长连接，不需要新建公网回调入口。私有目录中的 `state.sqlite3` 保留回复去重/每日模型配额，`actions.sqlite3` 保留确认状态；两者不是新的 Hub 业务数据库。重启时已抢占但未完成的写操作标记为未知，禁止盲目重试。`核对操作 XXXXXXXX` 可查看原操作状态与稳定编号。未知操作应在 Hub 人工核实，不重建任务绕过。

## 测试

```sh
python3 -m unittest discover -s . -p 'test_*.py' -q
```

测试默认使用合成数据、MockTransport 和隔离 SQLite，不需要真实飞书、企微或 DeepSeek 凭据，也不会真实发布或派发。真实平台写入必须由账号/应用所有者单独确认。
