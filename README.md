# QWBot

企业微信群机器人，用于推送贷款核算测试的跑批计划、执行进度文档链接和当前系统日期。

## 功能

- 支持企业微信群机器人 webhook 推送 markdown/text 消息。
- 支持立即发送、预览消息、webhook 连通性测试和常驻定时发送。
- 支持本地维护页面登记跑批计划。
- 支持从企业微信在线文档导出的 JSON/CSV 地址读取“跑批计划”和“执行进度”。
- 未配置在线文档地址时，默认从 `data/sample_status.json` 首次迁移到 SQLite，并以 `data/qwbot.sqlite3` 作为运行态数据。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

项目已创建本地 `.env`，其中包含当前群机器人的 webhook。`.env` 已加入 `.gitignore`，不要提交到仓库。可分别配置生产群和自测群机器人，通过 `WECOM_WEBHOOK_TARGET` 或 CLI 的 `--webhook test|prod` 选择发送目标。

预览将要发送的消息：

```powershell
python -m qwbot.cli preview
```

测试 webhook：

```powershell
python -m qwbot.cli test-webhook
```

立即发送日报提醒：

```powershell
python -m qwbot.cli send-now
```

手动执行一次定时任务逻辑：

```powershell
python -m qwbot.cli run-scheduled-once
```

`run-scheduled-once` 和真正的 09:00、18:00 定时任务一致，会检查是否为工作日、是否为自然节假日；如果不满足发送条件会跳过。`send-now` 则用于强制发送测试，不做工作日判断。

启动定时任务：

```powershell
python -m qwbot.cli schedule
```

启动本地维护页面：

```powershell
python -m qwbot.cli web
```

打开 `http://127.0.0.1:5000`，即可维护跑批计划。页面默认按自然日历、跑批前交易日期、跑批后交易日期升序展示未归档计划；新建和修改通过弹窗完成；只有“已完成”计划允许归档，“待执行”允许修改和删除，“进行中/有阻塞”只允许修改。状态改为“已完成”后当天仍保留在未归档列表，第二天打开页面时会自动归档。已归档记录为只读，支持分页、按自然日/交易日/节假日筛选和详情查看；进入已归档页时默认展示当天自然日的归档记录，如果当天无数据则展示最近一个有归档数据的自然日。状态为“有阻塞”时需要填写阻塞原因，并会自动展示在跑批计划下方的阻塞看板；每次阻塞会记录开始、关闭和持续时间，可在看板或归档详情中查看。未归档计划支持维护“计划执行时间”；点击“开始”会通知批量开始执行并 `@所有人`，点击“完成”会通知已完成批量和下一批量。运行态数据会写入 SQLite 数据库 `data/qwbot.sqlite3`，首次启动会从 `data/sample_status.json` 自动迁移；机器人预览、立即发送和定时发送都会读取未归档的跑批计划。

打开 `http://127.0.0.1:5000/notifications`，即可维护消息通知。原有组内重点工作提醒内容由系统生成，默认提供 09:00 和 18:00 两个发送时间点，并支持新增、修改、删除多个发送时间点；页面会展示今天和后续自然日，工作日默认提醒，周末和节假日默认不提醒，每个自然日都可手动设为不提醒或恢复提醒。自定义通知支持新增、修改、删除，可填写消息内容、在线文档链接和是否 `@所有人`。

如果需要给同事访问，可指定监听地址：

```powershell
python -m qwbot.cli web --host 0.0.0.0 --port 5000
```

定时任务默认每天 09:00 和 18:00 发送组内重点工作通知，可在维护页面维护多个发送时间点；周末、节假日和被设为不提醒的自然日会跳过固定工作提醒，手动恢复提醒的日期会照常发送。自定义消息通知按维护页面配置的时间发送。

```dotenv
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

1. 手动上传数据文件：

```sh
mkdir -p data
scp sample_status.json user@your-server:/path/to/QWBot/data/sample_status.json
```

1. 构建并启动：

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

`docker-compose.yml` 中 Web 服务监听容器内 `0.0.0.0:5000`，服务器通过 `QWBOT_PORT` 暴露端口；`scheduler` 服务单独运行定时任务，和 Web 服务共享 `./data:/app/data`。
