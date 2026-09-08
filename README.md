# 驾照科目一题库管理与模拟考试系统

基于 **Flask + MySQL 8.0** 的 B/S 架构课程实训项目：集成题库管理、模拟考试、顺序练习、错题本与多维统计，并实现了题目自动归类、重复题检测、网络题库采集、历年趋势对比、LLM 答案验证与 token 成本统计等加分功能。

v2 版本面向**课堂教学场景**扩展了 7 个教学功能（教师发布任务、学生答题计时、错题排行、数据导出、学生排名、答案解析、双人 PK 赛车游戏——赛道由 **Canvas 手绘引擎**渲染 F1 风格卡丁车竞速，含氮气尾焰、COMBO 连击、超车提示、冲线彩带与 Web Audio 全合成音效），并基于 **ECharts** 为统计页配置了 14 个交互式图表（扇形/玫瑰图、折线、柱状、仪表盘等）。账号体系支持**登录互踢**与管理员**用户管理**（删除/重置密码/改名）。

## 功能总览

### 核心功能
- **模拟考试**：随机组卷 100 题（判断题 1–40 + 单选题 41–100 分区展示），左侧答题卡实时标记作答状态，交卷自动判分生成成绩单
- **顺序练习**：每轮随机 10 题逐题作答即时反馈，答错自动进入错题本
- **错题本**：自动汇总错题及错误次数，支持"标记已掌握"移出
- **答案解析**：题目管理页可维护每题解析（失焦自动 AJAX 保存），交卷讲评时随错题展示
- **我的统计**：累计作答、正确率、最近考试成绩、个人易错题 TOP10（基于视图 `v_user_stat`）+ 正确率仪表盘/成绩走势折线/易错题条形图
- **全局统计（管理员）**：题库/用户/考试总量、题型分布、分类分布、每题答题量与正确率 TOP100（基于视图 `v_question_stat`）+ 14 天活跃趋势、仪表盘、堆叠柱状图等
- **记录管理**：考生可分别清除本人考试/练习/错题记录；管理员支持按用户清除或一键清空全体记录（二次确认 + 级联删除）
- **图片支持**：题目配图以 BLOB 存储（SHA-256 去重），由 `/image/<id>` 路由输出
- **登录互踢**：同一账号在新设备登录后旧会话自动失效（HTTP + Socket 双校验），杜绝两人共用账号导致练习/考试数据串号
- **用户管理（管理员）**：删除学生账号（级联清除考试/练习/错题/任务/PK 全部数据并关闭其所在对局）、重置密码为 123456（强制下线）、更正姓名；注册时必填真实姓名，排名与成绩公告直接展示

### 教学管理与游戏化扩展（v2）
| # | 功能 | 说明 |
|---|------|------|
| ① | **教师发布任务** | 教师按判断题/单选题数量抽题生成固定任务，选择"考试"（可勾选顺序打乱防作弊）或"练习/讲解"（全班同序）用途，自定义时限；支持关闭、重开、删除、编辑 |
| ② | **学生参加任务** | 学生首页显示"我的任务"；考试模式全屏倒计时、到点自动交卷；练习模式后端累计计时，支持暂停/继续、刷新页面状态恢复 |
| ③ | **答题数据导出** | 5 种场景（课堂讲评/成绩公告/个别辅导/教学反思/全量存档）× CSV / Excel 双格式；Excel 表头加粗、错题行标红、列宽自适应，文件名 RFC 5987 编码兼容中文 |
| ④ | **错题排行榜** | 全局或按任务筛选，统计错次、错误率、常见错误答案；配 TOP10 错误率条形图、题型玫瑰图、错误答案环形图、错误率区间饼图 |
| ⑤ | **学生排名** | 考试榜按平均分、练习榜按总正确题数；前三名奖牌、PK 段位徽章（青铜/白银/黄金/铂金）、连胜火焰；配成绩分段柱状图与 PK 胜负饼图 |
| ⑥ | **答案解析** | 见核心功能：题目管理维护解析，讲评页展示 |
| ⑦ | **双人 PK 赛车** | 基于 flask-socketio 的实时对战：发起方可自定义题型（判断题/单选题各若干，合计 10 题、交错出场）、15 秒/题、答对小车前进、3-2-1 倒计时、快捷表情包、胜利烟花、抢答答错直接送对方 1 分并跳下一题、胜方段位战绩更新；服务监听 `0.0.0.0` 支持公网/局域网双人联机 |

**数据可视化（ECharts，共 14 图）**：错题排行榜 4 图、全局统计 5 图、我的统计 3 图、学生排名 2 图，涵盖横向条形图、南丁格尔玫瑰图、环形/饼图、面积折线图、仪表盘、堆叠柱状图、渐变柱状图 7 种类型；echarts.min.js 本地引用，无外链依赖。

### 加分项实现
| # | 加分项 | 实现方式 | 位置 |
|---|--------|----------|------|
| ① | 题目自动归类 | 关键词规则 + jieba 分词 TF-IDF 最近质心两阶段分类，写入 `category` 表；采集新题入库后自动触发归类 | `classifier.py` |
| ② | 重复题检测 | 字符 2-gram 倒排索引筛候选 + 编辑距离/词级 Jaccard 精算 + 并查集聚簇 | `duplicate_detector.py` → `duplicate_report.md` |
| ③ | 答案正确性验证（LLM） | 盲测 + 仲裁三层漏斗（见下），支持任意数量/题型范围与图片题选项 | `app/llm.py` + "AI 验证"页 |
| ④ | 网络题库采集 | 公开题库源（DriverEasy/juhe）+ 指定 URL 爬取，后台线程执行、进度轮询、批次入库、新题自动归类；支持按年份版本删除采集数据（2026 原始题库受保护） | `app/crawler.py` + "题库采集"页 |
| ⑤ | 历年题库对比 | 按年份 × 题型/分类实时生成对比矩阵与自动趋势结论，"题库采集"页直接展示；命令行脚本可导出 md 报告 | `app/trend.py` + "题库采集"页、`trend_analysis.py` → `trend_report.md` |
| ⑥ | token 成本统计 | 每次调用记录 prompt/completion tokens 与耗时，按实际执行模型单价折算真实累计费用 | 同 ③，验证结果页汇总 |

### 答案验证算法：盲测 + 仲裁（三层漏斗）

```
第1层 规则校验   带图题（未启用视觉模型时）直接跳过，零成本
第2层 盲测       prompt 只含题干+选项（不给标准答案），AI 独立作答；
                 代码将 AI 答案归一化后与标准答案精确比对
                 ├─ 一致（约 90%）→ 判定"盲答一致"，无需第 3 层
                 └─ 分歧 → 进入仲裁
第3层 仲裁       仅分歧题：题目 + 标准答案 + AI 盲答一起交 AI 复审
                 ├─ 维持标准答案 → "仲裁维持"（AI 误答）
                 └─ 推翻标准答案 → "疑似错题"（含仲裁理由，供人工复核）
```

- 无锚定偏差：AI 看不到标准答案，验证结论可信；比对由代码完成，确定性无幻觉
- 并发执行（4 线程 + 限流退避），全量 3400+ 题约 1 小时跑完
- **图片题可选**：配置视觉模型（如 qwen-vl-plus）并勾选"包含图片题"后，819 道看图题从库中读取 BLOB 转 base64 走视觉模型真验证；不配置则自动跳过
- 每题答案归一化（全半角/字母/选项内容反查），兼容判断题与多选题

### 验证数据架构（三表分工）

| 表 | 职责 |
|---|---|
| `verify_batch` | 批次汇总：进度计数、状态、心跳（服务重启后僵死批次自动标记中断） |
| `verify_result` | 批次×题目明细（历史档案，永不覆盖，支持跨批次追溯与真实成本核算） |
| `ai_verification` | 每题每模型最新判定（供选题去重与最新状态展示） |

"AI 验证"页提供：API 配置（服务商预设/Base URL/Key/模型/视觉模型）+ 测试连接、
按范围与数量发起验证（数量留空即全量）、批次进度实时轮询、批次筛选结果明细、
Token 用量与费用汇总、一键导出验证报告（`answer_report.md`）、清空验证记录。

## 技术栈

- Python 3.10+ / Flask 3.x
- **flask-socketio**（双人 PK 实时通信，async 模式自动适配：开发机 threading / 生产 gevent）
- **gunicorn + gevent**（生产部署 WSGI，systemd 托管，见"云服务器部署"章节）
- MySQL 8.0（pymysql 驱动）
- **openpyxl**（Excel 导出：样式/标红/列宽）
- **ECharts 5.5**（前端图表，本地 `app/static/echarts.min.js` 引用）
- lxml、python-docx（题库解析）
- jieba（中文分词）
- openai SDK（LLM 验证，openai 兼容接口）
- requests（题库采集）

## 项目结构

```
├── app/
│   ├── main.py              # Flask 主应用：认证/考试/练习/任务/错题排行/导出/排名/PK(socketio)
│   ├── crawler.py           # 加分项④：题库采集（爬取/去重入库/自动归类/按年份删除）
│   ├── trend.py             # 加分项⑤：历年对比数据层（采集页与脚本共用）
│   ├── llm.py               # 加分项③⑥：盲测+仲裁验证 / 视觉模型 / token 成本统计
│   ├── static/
│   │   ├── style.css        # 全站样式（含图表卡片网格）
│   │   └── echarts.min.js   # ECharts 5.5 本地引用（图表无外链依赖）
│   └── templates/           # Jinja2 模板（含 v2 新增页）
│       ├── admin_tasks.html       # ① 教师任务发布/编辑/管理
│       ├── admin_wrong_rank.html  # ④ 错题排行榜 + 4 图表
│       ├── admin_answer_data.html # ③ 答题数据导出（5 场景 × CSV/Excel）
│       ├── admin_questions.html   # ⑥ 题目管理（答案解析编辑）
│       ├── ranking.html           # ⑤ 学生排名 + 分段柱状图/PK 饼图
│       ├── pk_lobby.html / pk_room.html  # ⑦ 双人 PK 大厅（题型配置）/ 赛车房间
│       ├── admin_stats.html / stats.html # 全局统计 5 图 / 我的统计 3 图
│       └── ...                    # 考试/练习/AI 验证/采集等
├── deploy/
│   ├── deploy.sh            # 云服务器一键部署（MySQL+venv+gunicorn+systemd）
│   └── update.sh            # 代码更新：拉取（自动降权）→ 同步依赖 → 重启 → 健康检查
├── sql/
│   ├── schema.sql           # 建库脚本（18 张基表 + v_user_stat / v_question_stat 视图）
│   ├── migration_v2.sql     # v2 增量迁移：task/task_record/pk_challenge 表 + 解析/PK 战绩字段
│   └── kemu1_exam_backup.sql.gz # 全量 mysqldump 备份（--hex-blob 导出，含题目与图片 BLOB）
├── docx_parser.py           # 解析 题库_2026.docx → 结构化题目（以"答案："为锚点切题）
├── importer.py              # 建表 + 题目/选项/图片批量入库（SHA-256 图片去重）
├── classifier.py            # 加分项①：题目自动归类
├── duplicate_detector.py    # 加分项②：重复题检测 → duplicate_report.md
├── trend_analysis.py        # 加分项⑤：历年趋势 → trend_report.md
├── data_cache/              # 采集数据的本地缓存（网络失败兜底）
└── 题库_2026.docx           # 原始题库素材（2308 题）
```

## 数据库设计

数据库 `kemu1_exam`，字符集 **utf8mb4**，存储引擎 **InnoDB**，共 **18 张基表 + 2 个视图**，按业务域分为 6 组。v2 新增表与字段的增量脚本见 `sql/migration_v2.sql`。

### 表关系总览

```
user ──< exam_paper ──< exam_detail >── question ──< option
  │        │  (task_id)                   │  ├── category（自关联 parent_id）
  │        └──< task_record >── task      │  ├── explanation（答案解析字段）
  ├──< practice >─────────────────────────┤  └──< question_image >── image
  ├──< wrong_book >───────────────────────┘
  └──< pk_challenge（challenger_uid / opponent_uid 双外键）

verify_batch ──< verify_result        llm_config（独立配置表）
ai_verification（question_id+model 唯一）   import_batch（采集批次）
```

### ① 用户域

| 表 | 用途 | 关键字段 |
|---|---|---|
| `user` | 账号与角色 | `username` 唯一、`password_hash`（SHA-256）、`role`(admin/student)、`real_name`；v2 新增 `pk_wins`/`pk_losses`/`win_streak` 战绩字段 |

### ② 题库域

| 表 | 用途 | 关键字段 |
|---|---|---|
| `question` | 题目主表 | `stem` 题干、`qtype` 枚举(judge/single/multi)、`category_id` 外键、`year_version` 年份版本（采集增量导入用）、`explanation` 答案解析（v2）；`ft_stem` 为 **ngram 全文索引**供重复题检索 |
| `option` | 选项 | `question_id` 外键（级联删除）、`label`(A/B/C/D/√/×)、`is_correct` 标记；唯一键 (question_id, label) |
| `image` | 图片 BLOB | `data` LONGBLOB、`content_hash`（SHA-256 唯一键，图片去重）、`ref_count` 引用计数 |
| `question_image` | 题-图多对多 | 联合主键 (question_id, image_id) + `position` 图序 |
| `category` | 知识分类 | `name` 唯一、`parent_id` 自关联外键支持层级分类 |

### ③ 答题域

| 表 | 用途 | 关键字段 |
|---|---|---|
| `exam_paper` | 考试试卷 | `user_id` 外键、`total_count`、`score` decimal(5,2)、`status`(in_progress/finished)、`started_at`/`submitted_at`；v2 新增 `task_id`（NULL=自由模拟考） |
| `exam_detail` | 试卷明细 | `paper_id` 外键（级联删除）、`question_id`、`seq_no` 卷内题号、`user_answer`、`is_correct`（未答为 NULL）；唯一键 (paper_id, seq_no)、(paper_id, question_id) 防重复落题 |
| `practice` | 顺序练习记录 | `user_id`+`question_id` 索引、`is_correct`、`practiced_at`（带时间索引供趋势统计） |
| `wrong_book` | 错题本 | (user_id, question_id) 唯一键、`wrong_count` 累计错次、`mastered` 是否已掌握 |

### ④ 教学任务域（v2 新增）

| 表 | 用途 | 关键字段 |
|---|---|---|
| `task` | 教师发布的任务 | `creator_uid`、`judge_count`/`single_count` 抽题数量、`time_limit_sec` 时限（NULL 不限）、`mode`(exam/practice)、`purpose`(exam 考试打乱 / review 讲解同序)、`question_ids` **发布时预生成的固定题目 ID 列表**、`shuffle_order`、`status`(draft/published/closed) |
| `task_record` | 学生参加记录 | (task_id, uid) 唯一键、`paper_id` 关联试卷、`status`(not_started/in_progress/completed/expired)、`start_time`/`submit_time`、`elapsed_sec` 实际用时、`paused`/`pause_time` 练习暂停状态 |

### ⑤ 双人 PK 域（v2 新增）

| 表 | 用途 | 关键字段 |
|---|---|---|
| `pk_challenge` | 对战记录 | `challenger_uid`/`opponent_uid` 双外键、`question_ids`（10 题 ID）、`status` 状态机(waiting→ready→playing→finished/declined)、`current_q` 当前题号、双方 `score`/`answers` 答题串、`winner_uid`；实时房间状态存服务进程内存，落库只保存题目与战绩 |

### ⑥ AI 验证与采集域

| 表 | 用途 | 关键字段 |
|---|---|---|
| `llm_config` | LLM 接口配置 | provider/base_url/api_key/model/`vl_model` 视觉模型（仅存数据库，不入仓库） |
| `verify_batch` | 验证批次 | 进度计数（total/done/correct_n/wrong_n/kept_n…）、`status` 含 interrupted（心跳超时自动标记） |
| `verify_result` | 批次×题目明细 | 历史档案**永不覆盖**，支持跨批次追溯与真实 token 成本核算 |
| `ai_verification` | 每题每模型最新判定 | (question_id, model) 唯一键、`verdict` 枚举(correct/kept/wrong/uncertain/skipped)、tokens 与耗时 |
| `import_batch` | 题库采集批次 | 来源类型/URL、`year_version`、fetched/imported/duplicates 计数、status |

### 视图（2 个）

- **`v_user_stat`**：学生练习统计——`practice` 按 user 聚合总作答/正确数算正确率，LEFT JOIN `wrong_book` 聚合错题总数与未掌握数
- **`v_question_stat`**：题目答题统计——`exam_detail` UNION ALL `practice` 后按题聚合答题量/正确量，LEFT JOIN 题目表算正确率（`NULLIF` 防除零）

### 设计要点

- **外键级联策略**：删试卷级联删明细、删题目级联删选项与题图关联；清除用户记录时 DELETE 主表（exam_paper/practice/wrong_book）即自动级联
- **枚举字段约束状态机**：role、qtype、试卷/任务/对战 status、verdict 均用 ENUM 限定取值
- **唯一键防重**：(paper_id, seq_no)、(task_id, uid)、(user_id, question_id) 等保证不重复落题/重复参加/重复错题
- **中文全文检索**：题干 FULLTEXT 索引使用 `WITH PARSER ngram`，支持中文二元分词查重
- **哈希去重**：密码 SHA-256 摘要存储；图片按 SHA-256 内容哈希去重，相同图片只存一份

## 快速开始

### 1. 准备数据库

方式 A：导入全量备份（推荐，含全部题目与图片数据，备份自带建库语句）：

```bash
gunzip -c sql/kemu1_exam_backup.sql.gz | mysql -u root -p --max-allowed-packet=512M
```

方式 B：仅建空库结构，再从 docx 重新导入：

```bash
# 解析 docx 生成结构化数据，然后建表并入库（需先安装依赖）
python docx_parser.py
python importer.py 你的MySQL密码
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
# 或手动安装：
pip install flask flask-socketio pymysql openpyxl lxml python-docx jieba openai requests
```

### 3. 修改数据库连接

连接参数通过环境变量配置：`DB_HOST` / `DB_USER` / `DB_PASSWORD` / `DB_NAME`（本机开发默认 `localhost / root / 123456 / kemu1_exam`，密码兼容旧变量 `MYSQL_PASSWORD`）；云服务器部署由 `/etc/kemu1.env` 统一注入，无需改代码。

### 4. 启动

```bash
python app/main.py
```

服务以 flask-socketio 模式启动，监听 `0.0.0.0:5000`（支持局域网访问，双人 PK 需两台设备分别访问本机 IP）。
访问 http://127.0.0.1:5000 ，内置管理员账号：**admin / admin123**（可注册考生账号）。

> 若从旧版升级，先执行 v2 增量迁移：`mysql -u root -p kemu1_exam < sql/migration_v2.sql`（新导入的全量备份已含全部表结构，无需迁移）。

## 云服务器部署（Ubuntu 22.04/24.04）

### 一键部署

```bash
git clone https://github.com/ming279/kemu1_exam.git
cd kemu1_exam
sudo bash deploy/deploy.sh
```

脚本自动完成：安装 MySQL 8 → 导入题库备份（2308 题 + 图片 BLOB）→ 创建专用数据库账号（随机密码）→ venv 安装依赖（含 gunicorn + gevent）→ 写入环境配置 `/etc/kemu1.env`（chmod 600）→ 注册 systemd 服务 `kemu1`（开机自启、崩溃重启）→ 防火墙放行 5000 → 健康检查并输出访问地址。

- 生产运行方式：`gunicorn --worker-class gevent -w 1 --bind 0.0.0.0:5000 main:app`。选用 gevent 是因为 gunicorn v23+ 已移除 eventlet worker，WebSocket 由 gevent 提供
- 腾讯云轻量服务器还需在**云控制台防火墙标签页**放行 TCP 5000 端口（系统内 ufw 与云端防火墙是两层）
- MySQL 备份使用 `--hex-blob` 导出，避免字符集转换损坏图片二进制

### 代码更新

```bash
sudo bash deploy/update.sh
```

拉取最新代码 → 同步依赖 → 重启服务 → 健康检查一条龙。脚本检测到 root 运行时会先把 `.git` 属主归还 ubuntu，并以 `sudo -H -u ubuntu` 降权执行 git pull / pip install，避免产生 root 属主文件导致后续 `git pull` 报 `insufficient permission`（root 只负责 systemctl 重启）。

## 加分项脚本使用

```bash
python classifier.py 你的MySQL密码        # ① 题目归类并回填 category_id
python duplicate_detector.py 你的MySQL密码 # ② 生成 duplicate_report.md
python trend_analysis.py                  # ⑤ 导出 trend_report.md（网页端可直接查看）
```

③④⑤⑥ 均可在网页端操作：管理员登录后使用导航栏"题库采集"（含历年对比趋势）与"AI 验证"页面。
AI 验证需在页面中填写服务商、Base URL、API Key、模型名（可选填视觉模型用于带图题；
保存至 `llm_config` 表，仅存数据库，不入代码仓库），支持先"测试连接"再发起批量验证任务。

## 数据规模

- 备份基线：**2308 题**（题库_2026.docx 导入），题目图片 757 张 / 819 道带图题（BLOB，SHA-256 去重）
- 网络采集功能实测可增量新增 1139 道历年题（2021/2022/2025 年份标记），采集后"历年题库对比"自动展示年份 × 题型/分类矩阵
- 重复题检测报告：194 对 / 145 簇（详见 `duplicate_report.md`，可重新运行生成）
- 自动归类全量覆盖（关键词规则 + TF-IDF）

## 功能与操作详解

### 角色与功能入口

| 角色 | 功能入口 |
|---|---|
| 管理员/教师 | 题目管理（解析维护）、任务管理（发布/编辑/关闭）、错题排行榜、答题数据导出、全局统计、题库采集、AI 验证、用户记录管理、用户管理（删除/重置密码/改名） |
| 学生 | 模拟考试、顺序练习、错题本、我的任务、我的统计、学生排名、PK 挑战 |

### 教师操作流程

1. **维护题库与解析**：「题目管理」分页浏览/关键词搜索，解析文本框失焦即 AJAX 自动保存，无需提交表单
2. **发布任务**：「任务管理」→ 填写标题、判断题数量、单选题数量、时限（分钟）、模式（考试/练习）、用途（考试=可打乱防作弊 / 讲解=全班同序）→ 发布时系统按题型 `ORDER BY RAND()` 抽出固定题集存入 `task.question_ids`
3. **任务管理**：列表可查看参加人数/完成情况；支持关闭（学生不可再进入）、重开、删除、编辑题量
4. **考后讲评**：「错题排行榜」按任务筛选，查看错次、错误率（红≥70%/黄40-70%/蓝<40% 三色分级）、常见错误答案，配合 4 个图表讲评；「答题数据」按场景导出 CSV/Excel
5. **教学反思**：「全局统计」查看 14 天活跃趋势、正确率仪表盘、分类薄弱点

### 学生操作流程

1. 登录后首页「我的任务」区块显示已发布任务及状态（未开始/进行中/已完成）
2. 进入任务：
   - **考试模式**：生成试卷与 `task_record`，顶部红色倒计时，到点自动交卷；不可暂停
   - **练习模式**：后端累计计时，可随时「暂停/继续」，暂停期间计时冻结；刷新/重开页面自动恢复进度与剩余状态
3. 交卷后自动判分：错题进入错题本，成绩单与题目解析可见
4. 「错题本」可反复练习，掌握后标记移出；「我的统计」查看正确率仪表盘与成绩走势
5. 「学生排名」查看双榜与段位；「PK 挑战」选择对手发起双人竞速对战

### 核心业务规则

| 规则 | 说明 |
|---|---|
| 模拟考组卷 | 判断题 40 + 单选题 60 = 100 题，与真实科目一一致；多选题仅在顺序练习出现 |
| 任务题集 | 发布时一次性抽题固定，全班答同一套题，保证讲评口径一致 |
| 防作弊打乱 | 用途为"考试"时，以学生 ID 为随机种子重排题目顺序（每人不同但自己两次进入一致），判断题始终排在单选题前；选项顺序不打乱；"讲解"用途全班同序 |
| 考试计时 | 前端倒计时 + 到点自动 submit 双保险；后端以 `submitted_at` 与 `elapsed_sec` 为准 |
| 练习计时 | `elapsed_sec` 累加已用时间段，暂停写 `pause_time`；恢复时用 `TIMESTAMPDIFF(COALESCE(pause_time,start_time), NOW())` 补当前段，刷新不丢时 |
| 判分规则 | 判断题 √/×、单选精确匹配、多选须完全一致；未答题 `is_correct=NULL` 计错但不记用户答案 |
| 错题本 | 练习/考试答错自动 upsert（`wrong_count+1`）；标记掌握置 `mastered=1` 移出活跃列表 |
| PK 对战 | 题型由发起方配置：判断题/单选题各若干、合计 10 题（大厅表单实时校验），抽题后交错出场、每题 15 秒；答对 +1 分并锁定该题（对手再答无效），抢答答错直接送对方 1 分并立即进入下一题；答完 10 题比总分，平分判平局（双方胜/负场均不变） |
| 登录互踢 | 同一账号仅一处在线：新登录覆盖 `user.login_token`，旧设备的 HTTP 请求与 Socket 事件校验失败即跳回登录页提示"已在其他设备登录" |
| 注册与姓名 | 注册必填账号/姓名/密码；姓名用于排名、成绩公告与个别辅导导出，管理员可在用户管理中随时更正 |
| 排名口径 | 考试榜 `AVG(score)` 降序、练习榜 `SUM(is_correct)` 降序 |
| 段位规则 | 🥉青铜 0-4 胜 ｜ 🥈白银 5-14 胜 ｜ 🥇黄金 15-29 胜 ｜ 💎铂金 30+ 胜；连胜 3 场显示 🔥 |

## 核心实现原理

### 1. 任务抽题与防作弊打乱

- **发布时固定题集**：`_generate_questions_for_task()` 按题型分别 `SELECT id FROM question WHERE qtype='judge' ORDER BY RAND() LIMIT n` 抽题，ID 列表逗号拼接存入 `task.question_ids`。全班同一套题，保证讲评口径一致
- **按人打乱顺序**：`_get_questions_for_student(task, uid)` 用 `random.Random(uid)` 作种子对题集 shuffle——同一学生每次进入顺序一致（断点续做），不同学生顺序不同（邻座看不到同号题）；判断题始终排在单选题前；选项顺序不打乱（避免答案错位）
- **讲解模式**：`purpose='review'` 不打乱，全班同序，便于课堂逐题讲评

### 2. 考试倒计时与练习计时

- **考试模式**：剩余秒数 `remain = time_limit_sec - TIMESTAMPDIFF(SECOND, start_time, NOW())`；前端红色倒计时，到点自动触发交卷表单 submit（前后端双保险，改本地时间无效）
- **练习模式（可暂停）**：`elapsed_sec` 保存已累计秒数，暂停时写 `pause_time`，恢复时计算当前段 `TIMESTAMPDIFF(SECOND, COALESCE(pause_time, start_time), NOW())` 累加；前端每 5 秒轮询 `/task/<id>/status` 同步，刷新页面/换设备登录都能恢复计时状态

### 3. socketio 双人 PK 实时对战

- flask-socketio **async 模式自动适配**：不写死 `async_mode`，装了 eventlet/gevent 的 Linux 生产环境走 gevent（配合 gunicorn），本机 Windows 开发自动回落 threading（`socketio.run(..., allow_unsafe_werkzeug=True)`），均监听 `0.0.0.0` 支持联机
- **房间状态**：内存字典 `PK_ROOMS`（room_key → 双方 uid/sid/得分/题号/ready 状态）；落库仅保存题目 ID 与最终战绩
- **题型配置**：发起挑战时大厅表单指定判断题/单选题数量（合计 10），服务端分别 `ORDER BY RAND()` 抽题后 `random.shuffle` 交错出场
- **事件流**：

```
connect → pk_join(join_room) → pk_ready（双方 ready 后广播 3-2-1-GO 倒计时）
   → _pk_next_question（每题 15 秒定时器，每秒检查 locked_q 标志）
   → pk_answer（服务端判分：答对 +1 分并置 locked_q 锁定该题；
               抢答答错送对方 1 分并锁定本题，双方立即进入下一题）
   → pk_emoji（快捷表情气泡）→ 10 题结束 _pk_finish
```

- **竞态控制**：题目推进逻辑只在定时器一处，答题触发锁定标志让 15 秒循环提前 break，避免"答完推进"与"超时推进"重复出题
- **判胜负**：比总分，胜者 `pk_wins+1, win_streak+1`，负者 `pk_losses+1, win_streak=0`，平局双方不变
- **断线处理**：disconnect 时遍历房间 emit 对手通知；题目数据在服务端保存（页面加载时不下发 `is_correct`，前端无法偷看答案）

### 4. 答题数据导出（5 场景 × 2 格式）

| 场景 | 数据口径 |
|---|---|
| review 课堂讲评 | 错题明细 + 题干/学生答案/正确答案/解析，按错次排序 |
| scores 成绩公告 | 每场考试用户名/得分/用时/提交时间 |
| tutor 个别辅导 | 按学生汇总错题与薄弱题型 |
| reflect 教学反思 | 高错误率题目聚合（错误率降序）供教师反思教学 |
| archive 全量存档 | 全部答题明细扁平表 |

- **CSV**：`utf-8-sig`（带 BOM），Excel 直接打开不乱码
- **Excel**（openpyxl）：表头加粗白字蓝底、错题行整行 `FEE2E2` 标红、列宽按内容自适应
- **中文文件名**：Content-Disposition 用 RFC 5987 `filename*=UTF-8''<urlencode>` + ASCII fallback（`export_<时间戳>.xlsx`），兼容各浏览器

### 5. ECharts 数据可视化

- 后端聚合查询后 `json.dumps(..., ensure_ascii=False)` 注入模板，前端 echarts.init 渲染；echarts.min.js 本地引用无外链
- 14 图分布：错题排行榜 4（TOP10 错误率条形/题型玫瑰图/错误答案环形/错误率区间饼）、全局统计 5（14 天双系列面积折线/正确率仪表盘/题型环形/分类条形/活跃堆叠柱）、我的统计 3（个人仪表盘/成绩走势折线带 90 分参考线/易错题条形）、排名 2（分数段渐变柱/PK 胜负环）
- 无数据场景显示友好占位文案（如"暂无 PK 对战记录"），窗口 resize 自动重绘

### 6. 关键工程经验

- **only_full_group_by 兼容**：明细查询中 `GROUP_CONCAT` 必须配合 GROUP BY；取每题正确答案改用关联子查询 `(SELECT GROUP_CONCAT(label) FROM option WHERE question_id=q.id AND is_correct=1)`
- **类型坑**：MySQL `SUM()/AVG()` 返回 Decimal，与 float 运算前需 `float()/int()` 转换
- **PyMySQL 格式化**：SQL 字符串中字面百分号必须写 `%%` 转义（如 `'高错误率(≥70%%)'`），否则报 "not enough arguments for format string"

## 说明

- 考试组卷比例：判断题 40 + 单选题 60，共 100 题，与真实科目一规则一致；多选题仅在顺序练习中出现
- 任务抽题在发布时固定题目列表：考试用途按学生 ID 做随机种子打乱顺序（判断题始终在前）防邻座作弊；讲解用途全班同序便于统一讲评
- PK 对战题型由发起方配置（判断/单选合计 10 题），抢答答错直接送对方 1 分并跳下一题；房间状态存于服务进程内存（生产 gevent / 开发 threading），服务重启后进行中的对战会中断，已完成战绩已落库不受影响
- 同一账号仅允许一处登录（登录互踢）；注册需填写真实姓名，管理员可在"用户管理"中删除账号（级联清除数据）、重置密码（重置为 123456 并踢下线）或更正姓名
- LLM 验证的图片题默认跳过；如需真验证，配置视觉模型（如 qwen-vl-plus）并勾选"包含图片题"即可
- API Key 仅保存在数据库 `llm_config` 表中，不会进入代码仓库
- 本项目为课程实训作品，仅供学习交流
