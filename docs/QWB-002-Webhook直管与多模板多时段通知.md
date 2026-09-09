# QWB-002 Webhook 直管与多模板多时段通知

> **需求编号**: QWB-002
> **创建日期**: 2026-09-09
> **状态**: 已完成

---

## 1. 需求背景

当前"群机器人 webhook"完全由 `.env` 环境变量决定，消息模板和通知时间虽然在数据库里，但都是**全局单例**，无法和具体的机器人绑定，存在以下问题：

1. 换群、加群必须改 `.env` 并重启容器（`docker compose up -d --force-recreate`），运维成本高，线上无法自助。
2. `WECOM_WEBHOOK_TARGET=prod|test` 是全局开关：切到自测群后，**所有**消息（固定提醒、自定义通知、批量开始/完成通知）都跑到自测群，无法做到"生产群发日报、自测群发联调消息"并存。
3. 消息模板表 `reminder_templates` 只允许一个 `is_active`，多模板实际只能"二选一"，不能同时按不同时间发到不同群。
4. 通知时间分散在三处且互不关联：`scheduler_times`（固定工作提醒时间）、`notification_tasks.send_time`（自定义通知时间）、`app_settings.reminder_template`（内容），三者无法组合成"某群 + 某模板 + 某几个时间点"。
5. 自测发送按钮硬编码 `settings.with_webhook_target("test")`，一旦 `.env` 没配 `WECOM_WEBHOOK_URL_TEST` 就直接抛异常。

**目标**：把 webhook 提升为一等公民，由维护页面直接管理；形成 **Webhook → 消息模板 → 通知时间** 的三级结构，`.env` 只作为首次初始化的种子和兜底。

---

## 2. 改造范围

### 1. 三级数据模型：Webhook → 模板 → 时间

**实现逻辑：**
- 新增 `webhooks` 表：一个机器人一行，字段 `name / url / target / is_enabled / is_default / remark / sort_order`。
- 新增 `message_templates` 表：`webhook_id` 外键指向 `webhooks`，一个 webhook 下可以挂任意多个模板，字段含 `name / content / at_all / date_rule / is_enabled / sort_order`。
- 新增 `template_schedules` 表：`template_id` 外键指向 `message_templates`，一个模板下可以挂任意多个发送时间点，字段含 `send_time / date_rule(可覆盖模板) / is_enabled`。
- 新增 `template_date_overrides` 表：按模板维度的"跳过/生效"自然日，替代原来只对固定提醒生效的 `notification_date_overrides`。
- 外键全部 `ON DELETE CASCADE`：删机器人级联删模板，删模板级联删时间和日期覆盖。

**关键设计：**
- **不复用** `reminder_templates` / `notification_tasks` 表，而是新建表 + 一次性数据迁移。原因：旧表的语义（全局唯一 active、时间挂在任务上）与新模型（时间挂在模板上、模板挂在机器人上）冲突，改列会破坏回滚能力；旧表原样保留，出问题可直接回退代码。
- `target` 字段保留 `prod / test / custom` 三值，用于兼容 CLI 的 `--webhook test|prod` 参数和"测试发送"按钮，但**不再是开关**，只是标签。
- 每个 webhook 有且仅有一个 `is_default`，批量开始/完成通知等"非模板类"即时消息走默认机器人。

**文件改动：**
- `qwbot/store.py`: 新增 4 张表、建表语句、CRUD 函数、`load_dispatch_plan()` 聚合查询
- `qwbot/web.py`: 新增 webhook / 模板 / 时间 / 日期覆盖 的路由
- `qwbot/templates/_webhook_section.html`: 新增三级嵌套卡片 UI
- `qwbot/static/style.css`: 新增嵌套卡片、时间胶囊、开关样式
- `qwbot/static/app.js`: 新增模板弹窗编辑器联动、时间行内编辑

---

### 2. `.env` 降级为"种子 + 兜底"

**实现逻辑：**
- `Settings` 新增 `webhook_seeds()`，把 `WECOM_WEBHOOK_URL_PROD` / `WECOM_WEBHOOK_URL_TEST` 转成两条待播种记录（含中文名和 `target`）。
- `init_store(db_path, migration_path, webhook_seeds=...)` 接收种子；`_ensure_schema` 里在 `webhooks` 表为空时写入，并用 `app_settings.webhook_model_seeded` 打标记，**只播种一次**。
- 运行期取 URL 时：`webhooks.url` 非空则以数据库为准；为空则按 `target` 回退到 `.env`（保证老部署升级后不断链）。
- 维护页面提供"从 .env 导入"按钮（`POST /webhooks/import-env`），部署到新服务器后可以一键把环境变量里的机器人补进数据库，按 URL 去重。

**关键设计：**
- 播种只在表为空时发生，避免用户在页面上删掉机器人后被环境变量"复活"。
- `.env` 里的 `WECOM_WEBHOOK_TARGET` 继续被 CLI `--webhook` 覆盖，但语义改为"选中数据库里 `target` 匹配的机器人"。
- 一条机器人都不存在时，播种一条 `url` 为空、`is_enabled=0` 的占位记录"默认群机器人"，保证页面有可编辑入口而不是空白。

**文件改动：**
- `qwbot/config.py`: 新增 `webhook_seeds()` / `env_webhook_url(target)`
- `qwbot/store.py`: `init_store` 增加 `webhook_seeds` 参数与播种逻辑
- `qwbot/web.py`: `create_app` 传入种子；新增 import-env 路由
- `qwbot/cli.py`: 各命令传入种子
- `.env.example` / `.env.deploy.example` / `README.md`: 说明环境变量新语义

---

### 3. 旧数据一次性迁移

**实现逻辑：**
迁移在 `_ensure_schema` 内执行，由 `app_settings.webhook_model_migrated` 标记，只跑一次，顺序如下：

1. 播种 webhook（见改造点 2）。
2. `reminder_templates` → `message_templates`：
   - `is_active=1` 的那条迁移为默认机器人下的模板「组内重点工作提醒」，`is_enabled=1`，`date_rule='business_day'`；
   - 其余模板同样迁移但 `is_enabled=0`，内容不丢；
   - 若 `reminder_templates` 为空，回退读 `app_settings.reminder_template`，再回退 `DEFAULT_REMINDER_TEMPLATE`。
3. `scheduler_times` → 上一步「组内重点工作提醒」模板的 `template_schedules`，`send_time` 原样保留（默认 09:00 / 18:00）。
4. `scheduler_skip_dates` / `scheduler_force_dates` → 该模板的 `template_date_overrides`。
5. `notification_tasks` → 每条自定义通知转成默认机器人下的一个模板：
   - 模板名 = 通知标题；
   - 模板内容 = 用 `_notification_task_to_content()` 还原成与旧 `build_custom_notification()` **逐字一致**的 markdown（含 `【标题】`、正文、`相关文档：` 列表、`<@all>`）；
   - `doc_url` 里的 `{var_name}` 引用在迁移时就用 `reminder_template_vars` 解析成真实链接，避免迁移后渲染不出；
   - `at_all` / `date_rule` 平移到模板；
   - `send_time` 变成该模板下的一条 `template_schedules`；
   - `notification_date_overrides` → `template_date_overrides`。

**关键设计：**
- 迁移只读旧表、只写新表，旧表数据一律保留，可回滚。
- 迁移过程整体包在一个 SQLite 事务里，中途失败不留半成品。
- 迁移幂等：重复调用（标记已存在）直接跳过。

**文件改动：**
- `qwbot/store.py`: `_migrate_to_webhook_model()`、`_notification_task_to_content()`

---

### 4. 统一消息渲染

**实现逻辑：**
- `message.py` 新增 `render_message_template(settings, template, template_vars, next_batch)`：
  1. 组装系统变量 `{title_date}` `{current_trading_date}` `{next_trading_date}`；
  2. 合并 `reminder_template_vars`（公共文档链接），空值回退 `.env` 同名 URL；
  3. 替换 `{var_name}` 占位符；
  4. `at_all=true` 且正文里没有 `<@all>` 时自动追加。
- `build_scheduled_reminder` 保留为兼容入口，内部委托给 `render_message_template`。
- `build_custom_notification` 保留（CLI 老命令和回归测试仍在用），但调度链路不再调用。

**关键设计：**
- 固定提醒和自定义通知共用同一个渲染器，模板能力对等，不再区分"系统模板"和"自定义通知"两套逻辑。
- 变量仍然是全局的（公共文档链接），不按 webhook 隔离——多个群引用同一份文档链接是常态，隔离会带来重复维护。

**文件改动：**
- `qwbot/message.py`: 新增 `render_message_template`，重构 `build_scheduled_reminder`
- `qwbot/store.py`: `DEFAULT_TEMPLATE_VARS` 保持不变，供渲染器回退

---

### 5. 调度器按三级结构派发

**实现逻辑：**
- `cli._send_due_notifications` 改为调用 `load_dispatch_plan(db_path)` 拿到"启用的机器人 → 启用的模板 → 启用的时间"扁平列表，逐项比对当前 `HH:MM`。
- 去重键升级为 `{date}:tpl:{template_id}:sch:{schedule_id}:{HH:MM}`，进程内 `sent_keys` 集合仍然只保留当天。
- 每个机器人的发送包在独立 `try/except` 里：一个 webhook 失效（被踢出群、key 过期）不影响其他机器人和后续时间点，异常打印到日志。
- 机器人 URL 为空时跳过并打印告警，不抛异常。

**发送判定（`should_dispatch`）优先级：**
1. 模板级 `template_date_overrides` 命中今天：`skip` 直接不发，`force` 直接发；
2. 全局 `scheduler_skip_dates` / `scheduler_force_dates` 命中今天：同上（仅对 `date_rule != 'all'` 的模板生效）；
3. 按生效的 `date_rule` 判定：`all` 恒发；`business_day` 需 `is_business_day()`；`holiday_only` 只跳过节假日（沿用旧语义）。
4. 时间点上的 `date_rule` 非空时覆盖模板级配置，支持"同一个模板工作日 09:00 发、周末 10:00 发"。

**文件改动：**
- `qwbot/cli.py`: 重写 `_send_due_notifications`、新增 `should_dispatch()`、`_dispatch_client()`
- `qwbot/store.py`: 新增 `load_dispatch_plan()`
- `qwbot/planner.py`: 无改动，复用 `is_business_day` / `is_holiday`

---

### 6. 维护页面重构

**实现逻辑：**
`/notifications` 页面重构为四个区域：

1. **群机器人**：卡片列表。卡片头显示名称、`target` 徽标、脱敏 URL（`key=abcd****wxyz`）、启用开关、默认徽标；操作有「测试连通性」「从 .env 导入」「编辑」「删除」「+ 新增机器人」。
2. **消息模板**（嵌在机器人卡片内）：每个模板一行，显示名称、启用开关、`@所有人`、发送范围、时间点数量；操作有「编辑内容」（弹窗）「测试发送」「复制」「删除」「+ 新增模板」。
3. **通知时间**（嵌在模板行内）：时间胶囊列表，每个胶囊可点击停用/启用、可删除；末尾「+ 新增时间」行内表单。
4. **提醒日期 / 公共文档链接**：沿用现有全局日期条和变量表，逻辑不变。

模板编辑弹窗复用现有编辑器能力：变量标签点击插入、样式片段、企业微信气泡预览、测试发送（发到**该模板所属机器人**，不再强制 test）。

**关键设计：**
- **完整 webhook key 从不出现在 HTML 里**：列表只显示脱敏值（`key=MOCK****0001`），编辑弹窗的地址输入框也**不回填**，留空表示"保持不变"，勾选"清空地址"才回退到 `.env` 兜底，填写新值则覆盖。这样截图、共享屏幕、查看网页源码都不会泄露 key。
- 删除机器人时二次确认文案明确提示会级联删除其下所有模板和时间点。
- 保留"至少一个机器人"的约束：最后一条机器人不允许删除，与旧"最后一个模板不允许删除"一致。
- 所有复选框都配一个同名 `hidden` 空值伴生字段，后端 `_flag()` 据此区分"未提交"（保持默认/原值）和"提交为未勾选"（明确关闭），避免程序化调用把新建的时间点意外创建成停用状态。
- 每次操作后用 `?notice=` / `?error=` + `#锚点` 重定向回页面，锚点直接定位到被操作的机器人/模板/时间。
- 旧的「固定工作提醒」「自定义通知」两个面板被三级结构取代；`notification_tasks` 相关路由下线。

**文件改动：**
- `qwbot/templates/notifications.html`: 引入新分区，新增 `notice` 提示条
- `qwbot/templates/_webhook_section.html`: 新增，三级嵌套结构 + 全部弹窗装配
- `qwbot/templates/_webhook_modal.html`: 新增，机器人新增/编辑弹窗
- `qwbot/templates/_template_modal.html`: 新增，模板编辑器弹窗（变量、样式片段、预览、测试发送）
- `qwbot/templates/_schedule_modal.html`: 新增，通知时间新增/修改弹窗
- `qwbot/templates/_notification_section.html`: 精简为「全局提醒日期 + 公共文档链接」
- `qwbot/templates/_notification_modal.html`: 删除（被模板弹窗取代）
- `qwbot/web.py`: 路由增删、`mask_webhook_url()`、`_flag()`、`_notification_redirect()`
- `qwbot/static/app.js`: 变量/样式片段/预览的 textarea 查找改为**同表单作用域**（一页多个模板弹窗），删除旧的单编辑器缓存、加载模板、另存为、自定义通知文档链接编辑器逻辑（658 → 380 行）
- `qwbot/static/style.css`: 新增三级卡片/时间胶囊/日期覆盖样式，清理已失效的通知面板样式（2361 → 2080 行，删除 101 条死规则）

---

### 7. 批量通知走默认机器人

**实现逻辑：**
- 跑批计划页的「开始」「完成」按钮原来直接用 `settings.webhook_url`，改为 `resolve_default_webhook_url(settings, db_path)`：优先取数据库默认机器人的 URL，为空回退 `.env`。
- CLI 的 `send-now` / `preview` / `test-webhook` / `run-scheduled-once` 同步改造。

**文件改动：**
- `qwbot/store.py`: 新增 `resolve_default_webhook_url()` / `resolve_webhook_url()`
- `qwbot/web.py`: `start_item` / `complete_item` 改用默认机器人
- `qwbot/cli.py`: 各命令改用解析函数

---

## 3. 技术细节

#### 表结构

```sql
CREATE TABLE webhooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT 'custom',   -- prod | test | custom
    is_enabled INTEGER NOT NULL DEFAULT 1,
    is_default INTEGER NOT NULL DEFAULT 0,
    remark TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE message_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_id INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    at_all TEXT NOT NULL DEFAULT '',
    date_rule TEXT NOT NULL DEFAULT 'business_day',
    is_enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (webhook_id) REFERENCES webhooks(id) ON DELETE CASCADE
);

CREATE TABLE template_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL,
    send_time TEXT NOT NULL DEFAULT '',
    date_rule TEXT NOT NULL DEFAULT '',      -- 空表示继承模板
    is_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (template_id) REFERENCES message_templates(id) ON DELETE CASCADE
);

CREATE TABLE template_date_overrides (
    template_id INTEGER NOT NULL,
    target_date TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT '',           -- skip | force
    created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (template_id, target_date),
    FOREIGN KEY (template_id) REFERENCES message_templates(id) ON DELETE CASCADE
);
```

#### `load_dispatch_plan` 返回结构

一次 SQL 联查后在 Python 侧组装，避免 N+1：

```python
[
  {
    "webhook_id": 1, "webhook_name": "生产群机器人", "url": "https://...",
    "template_id": 3, "template_name": "组内重点工作提醒",
    "content": "...", "at_all": "", "template_date_rule": "business_day",
    "schedule_id": 7, "send_time": "09:00", "schedule_date_rule": "",
    "skip_dates": ["2026-10-01"], "force_dates": [],
    "global_skip_dates": [...], "global_force_dates": [...],
  },
  ...
]
```

只返回 `webhooks.is_enabled=1 AND message_templates.is_enabled=1 AND template_schedules.is_enabled=1` 的组合。

#### URL 解析优先级

```
webhooks.url (非空)  >  .env WECOM_WEBHOOK_URL_{TARGET}  >  .env WECOM_WEBHOOK_URL  >  报错/跳过
```

#### URL 脱敏

```python
def mask_webhook_url(url: str) -> str:
    # https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcd1234-...-wxyz
    # -> https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcd****wxyz
```
key 长度不足 8 位时整体打码为 `****`。

#### SQLite 外键与级联

`sqlite3` 默认不开外键，`_connect()` 里已有 `PRAGMA foreign_keys = ON`，但 `PRAGMA` 是连接级的，必须在**每个连接**上执行——现有代码在 `_ensure_schema` 里执行过一次，级联删除实际不生效。本次改为在 `_connect()` 中统一执行，确保 `ON DELETE CASCADE` 真的生效。

#### 迁移事务

```python
with _connect(path) as connection:      # contextmanager 内 commit
    connection.execute("BEGIN")
    ...  # 播种 + 5 步迁移
    connection.execute(
        "INSERT INTO app_settings (key, value) VALUES ('webhook_model_migrated', 'true')"
    )
```

失败时 `contextmanager` 不提交，标记不落库，下次启动重试。

---

## 4. 影响范围

- **数据库**：新增 `webhooks`、`message_templates`、`template_schedules`、`template_date_overrides` 四张表；`app_settings` 新增 `webhook_model_seeded`、`webhook_model_migrated` 两个标记。旧表 `reminder_templates`、`reminder_template_vars`、`notification_tasks`、`notification_date_overrides`、`scheduler_times`、`scheduler_skip_dates`、`scheduler_force_dates` **全部保留**（`reminder_template_vars` 继续使用，其余仅作为迁移源和回滚兜底）。
- **配置**：`.env` 中 `WECOM_WEBHOOK_URL_PROD/TEST/TARGET` 从"唯一来源"降级为"首次播种 + URL 兜底"；不再需要为了换群重启服务。
- **API**：
  - 新增：`/webhooks`(POST)、`/webhooks/<id>`(POST)、`/webhooks/<id>/delete`、`/webhooks/<id>/toggle`、`/webhooks/<id>/set-default`、`/webhooks/<id>/test`、`/webhooks/import-env`、`/webhooks/<id>/templates`(POST)、`/templates/<id>`(POST)、`/templates/<id>/delete`、`/templates/<id>/toggle`、`/templates/<id>/duplicate`、`/templates/<id>/test-send`、`/templates/<id>/schedules`(POST)、`/schedules/<id>`(POST)、`/schedules/<id>/delete`、`/schedules/<id>/toggle`、`/templates/<id>/skip-date`、`/templates/<id>/force-date`
  - 下线：`/notifications*`、`/scheduler-times*`、`/reminder-template*`
  - 保留：`/scheduler-skip-dates*`、`/scheduler-force-dates*`、`/template-vars*`、`/items/*`
- **前端**：`/notifications` 页面结构重构；`_notification_modal.html` 删除，新增 `_webhook_section.html`、`_template_editor_modal.html`。
- **调度器**：派发逻辑从"两条并行链路（固定提醒 + 自定义通知）"改为"单一三级遍历"；单点失败隔离。
- **兼容性**：
  - 升级后首次启动自动迁移，无需人工干预；
  - `.env` 未配 `WECOM_WEBHOOK_URL_TEST` 时，"测试发送"退化为发到该模板所属机器人，不再抛异常；
  - 旧数据库文件可直接被新版本打开；
  - 回滚：还原代码即可，旧表数据未被修改或删除。

---

## 5. 测试要点

1. **首次迁移**：用旧版本生成的 `data/qwbot.sqlite3`（含 `reminder_templates` / `scheduler_times` / `notification_tasks`）启动新版本，确认机器人、模板、时间点、日期覆盖数量和内容一一对应，且迁移标记落库、重复启动不重复迁移。
2. **环境变量播种**：空库 + `.env` 配 prod/test 两个 URL 启动，确认生成两条机器人且 `is_default` 落在 `WECOM_WEBHOOK_TARGET` 指定的那条；`.env` 全空时生成一条停用占位机器人。
3. **Webhook CRUD**：新增/编辑/删除机器人；删除时级联删除其下模板和时间点；最后一条机器人禁止删除；页面 URL 脱敏展示、编辑时回填完整值。
4. **多模板**：同一机器人下新增多个模板，启用/停用互不影响；停用模板不参与调度。
5. **多时间点**：同一模板配置多个时间，各时间点独立启停；时间点级 `date_rule` 覆盖模板级配置生效。
6. **发送判定**：`all` / `business_day` / `holiday_only` 三种规则在周末、节假日、普通工作日的行为正确；模板级 skip/force 优先于全局 skip/force。
7. **渲染一致性**：迁移后的自定义通知模板渲染结果与旧 `build_custom_notification()` 输出逐字一致；系统变量和公共文档链接变量替换正确；`at_all` 自动追加 `<@all>` 且不重复追加。
8. **调度派发**：`run-scheduled-once` 在配置时间点上把消息发到正确的机器人；一个机器人 URL 非法时其他机器人照常发送，日志有告警。
9. **测试发送 / 连通性**：模板「测试发送」发到所属机器人；机器人「测试连通性」返回结果并在页面提示成功或失败原因。
10. **从 .env 导入**：已存在同 URL 时不重复创建；新 URL 追加为 `custom` 机器人。
11. **批量通知**：跑批「开始」「完成」消息发到数据库默认机器人；默认机器人切换后立即生效，无需重启。
12. **Docker 部署**：`docker compose up -d --build` 后 web + scheduler 两个容器共享 volume，页面上新增机器人后 scheduler 容器无需重启即可按新配置派发。
13. **自测群真实发送**：使用测试环境 webhook 完成一次真实推送，确认企业微信客户端渲染正确（标题、链接、颜色、`@所有人`）。
14. **回归**：跑批计划页的登记、修改、归档、阻塞看板、分页筛选功能不受影响。

---

## 6. 自测与验证

### 测试资产

| 文件 | 说明 |
| --- | --- |
| `tests/conftest.py` | `Settings` 构造、`init_db()`、**旧版 schema** 建表（用于真实迁移场景） |
| `tests/test_store_webhooks.py` | 播种、CRUD、级联、默认机器人、派发计划、日期覆盖 |
| `tests/test_migration.py` | 旧数据一次性迁移，含与旧 `build_custom_notification()` 的**逐字一致性**校验 |
| `tests/test_message_render.py` | 变量替换、`.env` 兜底、`<@all>` 追加、空内容回退默认模板 |
| `tests/test_dispatch_rules.py` | 三级日期判定优先级（模板覆盖 > 全局覆盖 > 发送范围） |
| `tests/test_scheduler_dispatch.py` | 派发循环、失败隔离、去重键、启停、无重启生效 |
| `tests/test_web_routes.py` | 全部 HTTP 路由（含错误分支、脱敏、旧路由已下线） |
| `scripts/mock-webhook.py` | 模拟企业微信机器人接口；`key` 以 `FAIL` 开头返回 `errcode 93000`，以 `HTTP500` 开头返回 500 |
| `scripts/selftest.py` | 端到端断言（50 项），支持 `--cli-command` 在容器内执行 CLI |
| `scripts/selftest.sh` | 一键自测：pytest + 临时 `.env`/DB + mock webhook + 临时 web 服务 |

### 执行结果

```sh
sh scripts/selftest.sh          # 92 passed + All 50 self-test assertions passed
```

- **单元测试**：`92 passed`
- **本地端到端**（venv + Flask 开发服务器 + mock webhook）：`50/50`
- **Docker 端到端**（`docker compose up -d` 后，web 用 gunicorn，CLI 通过 `docker compose exec -T scheduler` 执行，mock webhook 跑在宿主机、容器内经 `host.docker.internal` 访问）：`50/50`
- **常驻 scheduler 容器验证**：容器运行期间通过页面新增机器人「常驻调度验证群」和模板，2 分钟后到点自动发出，`scheduler` 容器日志：

  ```
  Scheduler started. 2 webhook(s) loaded from the SQLite database; notification times are configured per message template.
  Sent: 常驻调度验证群 / 常驻调度验证 at 13:20.
  ```

  证明**页面上改配置后 scheduler 容器无需重启即生效**。
- **失败隔离验证**：同时存在一个 `errcode 93000` 的失效机器人和一个正常机器人时，正常机器人照常收到消息，日志打印 `Failed: 失效群 / 失效模板: ...`，进程退出码 0。
- **页面结构校验**：渲染后的 `/notifications` 无未渲染的 Jinja 表达式；41 个表单 `action`/`formaction` 全部命中路由表；12 个 `data-open-modal` 全部有对应 `<dialog>`；4 个复选框均有 hidden 伴生字段；页面中不出现任何完整 webhook key。

### 测试环境真实 webhook 验证

用测试专用群机器人（`WECOM_WEBHOOK_URL_TEST`）跑通了真实推送，全部返回 `{"errcode":0,"errmsg":"ok"}`：

| 场景 | 入口 | 消息类型 | 结果 |
| --- | --- | --- | --- |
| webhook 连通性 | `python -m qwbot.cli test-webhook --webhook test` | text | `errcode 0` |
| webhook 连通性 | 页面「测试连通性」 | text | `errcode 0` |
| 迁移出的内置模板 | 页面模板「测试发送」 | markdown | `errcode 0`，`{title_date}`、四个文档链接变量、`<font color="info">` 全部正确替换 |
| markdown 全能力 + @所有人 | 新建模板「测试发送」 | markdown | `errcode 0`，覆盖标题/引用/加粗/三色文字/有序无序列表/链接/系统变量，`<@all>` 自动追加一次 |
| 机器人隔离 | prod 机器人指向本机 mock | text | mock 只收到 prod 的消息，真实测试群的消息没有串过去 |
| 常驻调度器 | `python -m qwbot.cli schedule` | markdown | 同一个模板挂 13:58（继承模板 `business_day`）和 13:59（时间点单独覆盖为 `all`）两个通知时间，到点各发一次：<br>`Sent: 自测群机器人 / 自测-定时派发校验 at 13:58.`<br>`Sent: 自测群机器人 / 自测-定时派发校验 at 13:59.` |

调度器进程在页面改完通知时间后启动，无需重启即按新时间派发；结合前面 Docker 场景（容器运行期间新增机器人后到点自动发送），确认**配置热生效**。

> 真实 key 只存在于 gitignore 忽略的本地 `.env`，提交前已用 `git diff --cached | grep` 全量扫描确认没有进入版本库。
