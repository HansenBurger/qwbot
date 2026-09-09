# QWBot

企业微信群机器人，用于推送贷款核算测试的跑批计划、执行进度文档链接和当前系统日期。

## 功能

- 支持企业微信群机器人 webhook 推送 markdown/text 消息。
- **群机器人（webhook）在维护页面直接管理**，不再依赖环境变量：一个机器人下可配置多个消息模板，一个模板下可配置多个通知时间。
- 支持立即发送、预览消息、按机器人测试连通性、按模板测试发送和常驻定时发送。
- 支持本地维护页面登记跑批计划。
- 支持从企业微信在线文档导出的 JSON/CSV 地址读取“跑批计划”和“执行进度”。
- 未配置在线文档地址时，默认从 `data/sample_status.json` 首次迁移到 SQLite，并以 `data/qwbot.sqlite3` 作为运行态数据。

## 通知模型（QWB-002）

消息通知是三级结构，全部保存在 SQLite 中，维护页面 `/notifications` 可以直接增删改：

```
群机器人 webhook
 └── 消息模板（可多个，各自独立启停、@所有人、发送范围）
      └── 通知时间（可多个，各自独立启停，可单独覆盖发送范围）
```

- 换群、加群、改时间都在页面上完成，**不需要改 `.env`，也不需要重启容器**；调度器每 30 秒重新读取一次配置。
- `.env` 里的 `WECOM_WEBHOOK_URL_PROD` / `WECOM_WEBHOOK_URL_TEST` 只用于两件事：数据库首次初始化时播种机器人（只播种一次），以及数据库里某个机器人地址留空时按 `prod`/`test` 标签兜底。页面上的“从 .env 导入”可以随时把环境变量里的地址补充进数据库（按地址去重）。
- `WECOM_WEBHOOK_TARGET` 决定首次播种时哪个机器人是默认的，以及 CLI `--webhook prod|test` 的兜底目标；它不再是全局开关，不同机器人可以同时往不同群发消息。
- 默认机器人用于跑批“开始/完成”等即时通知，可在页面上随时切换。
- 单个机器人失效（被移出群、key 过期）只会记录日志，不影响其他机器人和其他时间点。
- 升级到该版本时，原有的固定工作提醒时间、消息模板和自定义通知会自动迁移到新结构，旧表数据保留以便回滚。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Linux / macOS：

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

项目已创建本地 `.env`，其中包含当前群机器人的 webhook。`.env` 已加入 `.gitignore`，不要提交到仓库。首次启动会把 `.env` 里的地址播种成数据库中的机器人；之后请统一在 `/notifications` 页面维护。CLI 仍可用 `--webhook test|prod`（按标签选中数据库里的机器人）、`--webhook-id`、`--template` 精确指定发送目标。

预览将要发送的消息：

```powershell
python -m qwbot.cli preview
```

测试 webhook（默认机器人，可用 `--webhook` / `--webhook-id` 指定）：

```powershell
python -m qwbot.cli test-webhook
python -m qwbot.cli test-webhook --webhook prod
```

立即发送消息模板（默认机器人下所有启用的模板，可用 `--template` 只发一个）：

```powershell
python -m qwbot.cli send-now
python -m qwbot.cli send-now --webhook-id 3 --template 7
```

手动执行一次定时任务逻辑：

```powershell
python -m qwbot.cli run-scheduled-once
python -m qwbot.cli run-scheduled-once --at 09:00
```

`run-scheduled-once` 和真正的定时任务一致，会遍历“启用的机器人 × 启用的模板 × 启用的通知时间”，并检查发送范围、工作日、自然节假日和日期覆盖；不满足条件会跳过。`--at HH:MM` 可以用指定时间代替当前时间，便于自测。`send-now` 用于强制发送测试，不做日期判断。

启动定时任务：

```powershell
python -m qwbot.cli schedule
```

启动本地维护页面：

```powershell
python -m qwbot.cli web
```

打开 `http://127.0.0.1:5000`，即可维护跑批计划。页面默认按自然日历、跑批前交易日期、跑批后交易日期升序展示未归档计划；新建和修改通过弹窗完成；只有“已完成”计划允许归档，“待执行”允许修改和删除，“进行中/有阻塞”只允许修改。状态改为“已完成”后当天仍保留在未归档列表，第二天打开页面时会自动归档。已归档记录为只读，支持分页、按自然日/交易日/节假日筛选和详情查看；进入已归档页时默认展示当天自然日的归档记录，如果当天无数据则展示最近一个有归档数据的自然日。状态为“有阻塞”时需要填写阻塞原因，并会自动展示在跑批计划下方的阻塞看板；每次阻塞会记录开始、关闭和持续时间，可在看板或归档详情中查看。未归档计划支持维护“计划执行时间”；点击“开始”会通知批量开始执行并 `@所有人`，点击“完成”会通知已完成批量和下一批量。运行态数据会写入 SQLite 数据库 `data/qwbot.sqlite3`，首次启动会从 `data/sample_status.json` 自动迁移；机器人预览、立即发送和定时发送都会读取未归档的跑批计划。

打开 `http://127.0.0.1:5000/notifications`，即可维护群机器人和消息通知：

- **群机器人**：新增、编辑、启用/停用、设为默认、测试连通性、删除。页面上 webhook 地址一律脱敏展示（`key=MOCK****0001`），编辑弹窗也不回填完整 key，留空表示保持不变，勾选“清空地址”才会退回 `.env` 兜底。
- **消息模板**：每个机器人下可新增任意多个模板，支持模板名、消息内容、`@所有人`、发送范围（所有自然日 / 跳过周末和节假日 / 仅跳过节假日）、启用开关、复制和删除。模板编辑器保留变量标签点击插入、样式片段和企业微信气泡预览。
- **通知时间**：每个模板下可配置多个时间点，逐个启用/停用、修改、删除，并可为单个时间点单独指定发送范围（例如工作日 09:00、周末 10:00）。
- **日期覆盖**：模板级可对某个自然日单独“跳过”或“生效”，优先级高于全局提醒日期。
- **全局提醒日期**：展示今天和后续自然日，工作日默认提醒，周末和节假日默认不提醒，可整体跳过或恢复。
- **公共文档链接**：全局变量，所有机器人的模板都可以用 `{变量名}` 引用。

删除机器人会级联删除其下所有模板和通知时间，最后一条机器人不允许删除。

如果需要给同事访问，可指定监听地址：

```powershell
python -m qwbot.cli web --host 0.0.0.0 --port 5000
```

定时任务每 30 秒检查一次，把当前 `HH:MM` 与所有“启用的机器人 × 启用的模板 × 启用的通知时间”比对，命中即渲染并发送到对应机器人。默认迁移出 09:00 和 18:00 两个时间点；周末、节假日、被全局跳过的自然日以及被模板单独跳过的日期会按发送范围跳过，手动“生效”的日期照常发送。同一个进程内同一时间槽只会发送一次。

```dotenv
# 仅在数据库首次初始化时播种机器人，以及机器人地址留空时兜底
WECOM_WEBHOOK_URL_PROD=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=your-prod-key
WECOM_WEBHOOK_URL_TEST=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=your-test-key
WECOM_WEBHOOK_TARGET=test
PROGRESS_DOC_URL=https://doc.weixin.qq.com/sheet/your-progress-doc
BATCH_REGISTER_DOC_URL=https://doc.weixin.qq.com/sheet/your-batch-register-doc
CASE_ASSIGNMENT_DOC_URL=https://doc.weixin.qq.com/sheet/your-case-assignment-doc
AGENDA_DOC_URL=https://doc.weixin.qq.com/sheet/your-agenda-doc
FRONTEND_URL=http://127.0.0.1:5000
QWBOT_DB_PATH=data/qwbot.sqlite3
```

组内重点工作通知会包含进度统计、用例分工、跑批计划、加班申请四个链接，以及交易日信息和前端登记链接；自定义消息通知内容由维护页面配置。

## 在线文档数据格式

如果企业微信在线文档可以发布为 JSON 或 CSV 地址，分别填入：

```dotenv
BATCH_PLAN_SOURCE_URL=https://example.com/batch-plan.csv
PROGRESS_SOURCE_URL=https://example.com/progress.csv
```

CSV 表头支持以下任一组合：

```csv
内容,负责人,状态
贷款核算日切前数据准备,测试负责人,待执行
跑批计划一：合同计提、利息核算、账务汇总校验,批量执行人,计划中
```

JSON 支持数组或对象：

```json
{
  "batch_plan": [
    {"content": "贷款核算日切前数据准备", "owner": "测试负责人", "status": "待执行"}
  ],
  "progress": [
    {"content": "登记今日执行进度", "owner": "全体测试成员", "status": "待更新"}
  ]
}
```

## 还需要你补充的信息

企业微信在线文档通常存在权限控制。要从真实在线文档自动读取，需要补充以下一种方式：

- 已发布且机器人运行环境可访问的 CSV/JSON 地址。
- 企业微信应用的 `corp_id`、`corp_secret`、文档 ID、表格/工作表 ID、读取范围，并确保应用有读取文档权限。

当前跑批登记簿分享页可用于人工点击查看，但程序直接请求时只返回前端 HTML，不包含“小环境跑批日历”和“长周期跑批日历”的单元格数据。自动取数需要使用企业微信文档接口，例如“获取表格数据”接口，配置项如下：

```dotenv
BATCH_REGISTER_DOC_ID=e3_AOoAMQZ-ABwCNfgb04nBZTqiVzfaO
BATCH_SMALL_ENV_SHEET_ID=bku6h2
BATCH_LONG_CYCLE_SHEET_ID=
BATCH_SMALL_ENV_RANGE=A1:Z1000
BATCH_LONG_CYCLE_RANGE=A1:Z1000
```

还需要补充“长周期跑批日历”的工作表 ID。可以切换到该标签页后复制 URL 里的 `tab=` 参数。

在这些信息补齐前，机器人会在首次启动时把 `data/sample_status.json` 迁移到 `data/qwbot.sqlite3`，保证推送流程可以先跑通。

## 本地自测

不依赖真实企业微信群的完整自测（单元测试 + 用 mock webhook 跑通页面和调度）：

```sh
sh scripts/selftest.sh          # pytest + 端到端
sh scripts/selftest.sh --unit   # 只跑 pytest
sh scripts/selftest.sh --live   # 只跑端到端
```

`scripts/selftest.sh` 会在 `tmp/selftest/` 下准备一次性 `.env` 和 SQLite 数据库，启动 `scripts/mock-webhook.py`（模拟企业微信机器人接口，`key` 以 `FAIL` 开头返回 `errcode 93000`，以 `HTTP500` 开头返回 500）和一个临时 web 服务，然后由 `scripts/selftest.py` 校验：环境变量播种、页面脱敏、机器人/模板/时间 CRUD、按机器人隔离投递、坏 webhook 不影响其他机器人、启停生效、调度命中、CLI `preview`/`send-now`/`test-webhook`、级联删除和“最后一个机器人不可删除”。

单独启动 mock webhook：

```sh
python3 scripts/mock-webhook.py --host 127.0.0.1 --port 8089
curl http://127.0.0.1:8089/__messages      # 查看收到的消息
curl -X POST http://127.0.0.1:8089/__reset # 清空
```

真实自测群验证：把 `.env` 里的 `WECOM_WEBHOOK_URL_TEST` 换成测试群机器人的真实地址，启动后在页面上对该机器人点“测试连通性”，再对模板点“测试发送”。

## 本地 Docker 部署

本地用 Docker 跑整套服务（web + scheduler），用 `scripts/local-deploy.sh`，子命令风格和 `scripts/dev.sh` 一致：

```sh
sh scripts/local-deploy.sh deploy      # 构建镜像（已存在则跳过）并启动，幂等
sh scripts/local-deploy.sh rebuild     # 不用缓存重新构建并重建容器
sh scripts/local-deploy.sh restart     # 只重启容器，不重新构建
sh scripts/local-deploy.sh stop        # 停止并移除容器，保留 .env 和 data/
sh scripts/local-deploy.sh logs        # 跟随 web + scheduler 日志
sh scripts/local-deploy.sh logs 50     # 只打印最后 50 行后退出
sh scripts/local-deploy.sh logs scheduler 50
sh scripts/local-deploy.sh status      # 容器/镜像/端口/数据库 + 机器人、模板、通知时间概览
sh scripts/local-deploy.sh url         # 打印访问地址并探测连通性
sh scripts/local-deploy.sh shell       # 进入 web 容器
sh scripts/local-deploy.sh dispatch    # 在容器内手动执行一次定时派发
sh scripts/local-deploy.sh dispatch 09:00   # 用 09:00 代替当前时间，模拟到点
sh scripts/local-deploy.sh reset-db    # 停容器并删库，下次 deploy 按 .env 重新播种
```

和服务器用的 `scripts/deploy.sh` 的区别：

- 不要求 `data/sample_status.json`。本地空库即可启动，机器人会从 `.env` 播种，跑批计划在页面上登记。
- `.env` 不存在时自动从 `.env.example` 复制，之后可以先启动、再到 `/notifications` 页面新增真实机器人。
- `QWBOT_PORT` 被其他进程占用时自动往后找空闲端口，并同步改写 `.env` 里的 `QWBOT_PORT` 和 `FRONTEND_URL`（与 `scripts/dev.sh` 行为一致）。
- 自动规避 Docker Desktop 在 WSL 下的凭据助手问题：`~/.docker/config.json` 里的 `credsStore: desktop.exe` 在 Linux 侧无法执行（`exec format error`），会让构建卡在拉取基础镜像。脚本检测到后只在本次运行内改用 `tmp/docker-config`（内容 `{}`），**不会修改你的全局 docker 配置**。设置 `QWBOT_DOCKER_CONFIG_FORCE=1` 可跳过该处理。
- `status` 会直接读容器内数据库，打印每个机器人的脱敏地址、是否默认/停用，以及每个模板下配置的通知时间，方便确认"页面上改的配置调度器是否已经看到"。

`data/qwbot.sqlite3` 由容器内 root 创建，宿主机一般仍可删除（`data/` 目录属于你）；万一删不掉，`reset-db` 会自动改用一次性容器删除。

改完代码想让容器生效用 `rebuild`；只改了页面上的机器人/模板/通知时间**什么都不用做**，scheduler 每 30 秒重读数据库。

## Linux Docker 部署

服务器部署时不要把 `data/sample_status.json` 或 `data/qwbot.sqlite3` 打进镜像，也不要提交到 Git。旧 JSON 可由你手动上传到服务器项目目录的 `data/sample_status.json`，容器首次启动会迁移到同一 volume 下的 SQLite 数据库。

1. 在服务器拉取代码后初始化目录和配置：

```sh
sh scripts/bootstrap-server.sh
```

1. 编辑服务器上的 `.env`，至少补充：

```dotenv
WECOM_WEBHOOK_URL_PROD=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=your-prod-key
WECOM_WEBHOOK_URL_TEST=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=your-test-key
WECOM_WEBHOOK_TARGET=prod
FRONTEND_URL=http://your-server-ip:5000
QWBOT_PORT=5000
QWBOT_DB_PATH=data/qwbot.sqlite3
```

  首次启动后，机器人和通知配置都保存在 `data/qwbot.sqlite3` 里，之后在 `/notifications` 页面维护即可，改 `.env` 不会再生效（除非在页面上点“从 .env 导入”或把机器人地址清空）。

1. 手动上传数据文件：

```sh
mkdir -p data
scp sample_status.json user@your-server:/path/to/QWBot/data/sample_status.json
```

1. 构建并启动（服务器部署用 `deploy.sh`，本地开发用上面的 `local-deploy.sh`）：

```sh
sh scripts/deploy.sh
```

1. 查看日志：

```sh
sh scripts/logs.sh
sh scripts/logs.sh web
sh scripts/logs.sh scheduler
```

1. 手动测试定时任务逻辑：

```sh
sh scripts/test-scheduled.sh
```

1. 停止服务：

```sh
sh scripts/stop.sh
```

`docker-compose.yml` 中 Web 服务监听容器内 `0.0.0.0:5000`，服务器通过 `QWBOT_PORT` 暴露端口；`scheduler` 服务单独运行定时任务，和 Web 服务共享 `./data:/app/data`。因为调度器每 30 秒重新读取数据库，页面上新增或修改机器人、模板、通知时间后 `scheduler` 容器**不需要重启**即可生效。
