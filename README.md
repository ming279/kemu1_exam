# 驾照科目一题库管理与模拟考试系统

基于 **Flask + MySQL 8.0** 的 B/S 架构课程实训项目：集成题库管理、模拟考试、顺序练习、错题本与多维统计，并实现了题目自动归类、重复题检测、网络题库采集、历年趋势对比、LLM 答案验证与 token 成本统计等加分功能。

## 功能总览

### 核心功能
- **模拟考试**：随机组卷 100 题（判断题 1–40 + 单选题 41–100 分区展示），左侧答题卡实时标记作答状态，交卷自动判分生成成绩单
- **顺序练习**：每轮随机 10 题逐题作答即时反馈，答错自动进入错题本
- **错题本**：自动汇总错题及错误次数，支持"标记已掌握"移出
- **我的统计**：累计作答、正确率、最近 10 次考试成绩、个人易错题 TOP10（基于视图 `v_user_stat`）
- **全局统计（管理员）**：题库/用户/考试总量、题型分布、分类分布、每题答题量与正确率 TOP100（基于视图 `v_question_stat`）
- **记录管理**：考生可分别清除本人考试/练习/错题记录；管理员支持按用户清除或一键清空全体记录（二次确认 + 级联删除）
- **图片支持**：题目配图以 BLOB 存储（SHA-256 去重），由 `/image/<id>` 路由输出

### 加分项实现
| # | 加分项 | 实现方式 | 位置 |
|---|--------|----------|------|
| ① | 题目自动归类 | 关键词规则 + jieba 分词 TF-IDF 最近质心两阶段分类，写入 `category` 表 | `classifier.py` |
| ② | 重复题检测 | 字符 2-gram 倒排索引筛候选 + 编辑距离/词级 Jaccard 精算 + 并查集聚簇 | `duplicate_detector.py` → `duplicate_report.md` |
| ③ | LLM 答案验证 | 管理员在网页配置 openai 兼容接口（智谱/DeepSeek/Kimi/通义/OpenAI 预设），后台线程逐题让 AI 独立作答并与标准答案比对 | `app/llm.py` + "AI 验证"页 |
| ④ | 网络题库采集 | 公开题库源（DriverEasy/juhe）+ 指定 URL 爬取，后台线程执行、进度轮询、批次入库（实测新增 2021/2022/2025 年题目 1139 道） | `app/crawler.py` + "题库采集"页 |
| ⑤ | 历年题库对比 | 按年份 × 题型/分类生成对比矩阵与趋势结论 | `trend_analysis.py` → `trend_report.md` |
| ⑥ | token 成本统计 | 每次验证记录 prompt/completion tokens 与耗时，按服务商单价估算费用 | 同 ③，验证结果页汇总 |

## 技术栈

- Python 3.10+ / Flask 3.x
- MySQL 8.0（pymysql 驱动）
- lxml、python-docx（题库解析）
- jieba（中文分词）
- openai SDK（LLM 验证，openai 兼容接口）
- requests（题库采集）

## 项目结构

```
├── app/
│   ├── main.py              # Flask 主应用：认证/考试/练习/错题本/统计/图片服务
│   ├── crawler.py           # 加分项④：题库采集模块
│   ├── llm.py               # 加分项③⑥：LLM 答案验证 + token 成本统计
│   ├── static/style.css     # 全站样式
│   └── templates/           # Jinja2 模板（考试分区答题卡/AI 验证/采集管理等 15 页）
├── sql/
│   ├── schema.sql           # 建库脚本（14 张表 + v_user_stat / v_question_stat 视图）
│   └── kemu1_exam_backup.sql# 全量 mysqldump 备份（含 3449 题与 759 张图片 BLOB）
├── docx_parser.py           # 解析 题库_2026.docx → 结构化题目（以"答案："为锚点切题）
├── importer.py              # 建表 + 题目/选项/图片批量入库（SHA-256 图片去重）
├── classifier.py            # 加分项①：题目自动归类
├── duplicate_detector.py    # 加分项②：重复题检测 → duplicate_report.md
├── trend_analysis.py        # 加分项⑤：历年趋势 → trend_report.md
├── data_cache/              # 采集数据的本地缓存（网络失败兜底）
└── 题库_2026.docx           # 原始题库素材（2308 题）
```

## 快速开始

### 1. 准备数据库

方式 A：导入全量备份（推荐，含全部题目与图片数据）：

```sql
CREATE DATABASE kemu1_exam DEFAULT CHARACTER SET utf8mb4;
```

```bash
mysql -u root -p kemu1_exam < sql/kemu1_exam_backup.sql
```

方式 B：仅建空库结构，再从 docx 重新导入：

```bash
# 解析 docx 生成结构化数据，然后建表并入库（需先安装依赖）
python docx_parser.py
python importer.py 你的MySQL密码
```

### 2. 安装依赖

```bash
pip install flask pymysql lxml python-docx jieba openai requests
```

### 3. 修改数据库连接

`app/main.py` 与各脚本中的连接参数默认为 `root / 123456 / kemu1_exam`，按需修改（支持环境变量 `MYSQL_PASSWORD`）。

### 4. 启动

```bash
python app/main.py
```

访问 http://127.0.0.1:5000 ，内置管理员账号：**admin / admin123**（可注册考生账号）。

## 加分项脚本使用

```bash
python classifier.py 你的MySQL密码        # ① 题目归类并回填 category_id
python duplicate_detector.py 你的MySQL密码 # ② 生成 duplicate_report.md
python trend_analysis.py                  # ⑤ 生成 trend_report.md
```

③④⑥ 在网页端操作：以管理员登录后使用导航栏"题库采集"与"AI 验证"页面。
AI 验证需在页面中填写服务商、Base URL、API Key 与模型名（保存至 `llm_config` 表，
仅存数据库，不入代码仓库），支持先"测试连接"再发起批量验证任务。

## 数据规模（当前）

- 题库总量 **3449 题**（docx 导入 2308 + 网络采集新增 1139，含 2021/2022/2025 年份标记）
- 题目图片 759 张（BLOB，SHA-256 去重）
- 重复题检测：194 对 / 145 簇（详见 `duplicate_report.md`）
- 自动归类覆盖率 3449 题（关键词规则 + TF-IDF）

## 说明

- 考试组卷比例：判断题 40 + 单选题 60，共 100 题，与真实科目一规则一致
- LLM 验证对"看图题"（标志/信号灯/仪表）会如实返回"无法判定"（纯文本模型无法识图），属预期行为
- 本项目为课程实训作品，仅供学习交流
