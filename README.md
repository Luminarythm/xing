# Xing · GEO MVP

面向中国大陆 AI 产品曝光研究的本地单运营者工程。Python 3.12+ 标准库 + SQLite，无第三方运行依赖、无需模型密钥。**当前交付为 REST API 工程骨架，没有前端看板；国内平台实采尚未接入。**

## 快速运行（Windows PowerShell）

```powershell
cd xing
$env:GEO_API_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python -m geo
```

另开终端，将 `GEO_API_TOKEN` 设置为同一个值，然后：

```powershell
python scripts/demo.py
python -m unittest discover -s tests -v
```

Linux/macOS：`export GEO_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"`，随后同样执行 `python -m geo`。运行时数据库默认 `data/geo.sqlite3`；可用 `GEO_DB`、`GEO_PORT` 覆盖。进程自动建表；重启保留数据。

演示通过真实 HTTP 接口登记站点、导入两条模拟爬虫日志和一条模拟回答，再输出报告；预期声明 AI 请求 2、失败请求 1、品牌提及样本 1、本站引用样本 1。重复运行不会重复计数。演示数据明确标为 synthetic，不代表真实产品表现。

## 已实现

- 站点登记：站点名称、来源域名、品牌/竞品名称与别名列表。
- 日志采集接收：标准 JSON、Nginx JSON 两个适配器，站点范围校验、批次事务、事件去重。
- AI 回答：平台、模型、采样入口、提示词、回答、引用 URL、证据链接、时间、地区语言。
- 基础报告：声明 AI 请求、失败请求、观察到的页面、品牌提及、本站引用及样本引用率。
- 证据读取：原始回答及规范化日志可通过 events 接口追溯；URL 查询参数、IP、原始 UA 不入库。
- Bearer 鉴权、2 MB 请求限制、1000 条批次限制、UTC 规范化、数据库版本和自动测试。
- HTTPS 静态文件所有权挑战、验证有效期、站点专属日志密钥、密钥撤销/轮换。
- Nginx JSONL 文件采集器：持久 outbox、断点、重试去重、保留文件的 rename 轮转；详见 [接入指南](docs/INGESTION.md)。
- 版本化爬虫证据目录、传统搜索/AI分类，以及不可变页面清单快照、观察覆盖率和2xx成功覆盖率。
- 品牌/竞品与别名配置版本、人工提及复核审计；平台/入口/模型/来源筛选和CSV导出。
- 事件游标分页、保留删除预览、备份/新文件恢复；可恢复的本地mock采样任务。

## 接口

除 `GET /health` 外均需 `Authorization: Bearer <GEO_API_TOKEN>`。请求/响应为 JSON，CSV下载除外。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST / GET | `/api/v1/sites` | 创建 / 分页列出站点items/next_cursor |
| POST | `/api/v1/sites/{id}/logs` | 批量日志 |
| POST | `/api/v1/sites/{id}/answers` | 批量回答与引用 |
| GET | `/api/v1/sites/{id}/report` | 基础统计 |
| GET | `/api/v1/sites/{id}/events` | 分页证据对象items/next_cursor（格式变更） |
| GET | `/api/v1/crawlers` | 爬虫目录、证据和待核实项 |
| GET | `/api/v1/status` | 管理员运行状态、任务积压与生效容量限制 |
| POST / GET | `/api/v1/sites/{id}/inventory` | 导入 / 读取页面清单快照 |
| POST / GET | `/api/v1/sites/{id}/brands` | 保存 / 读取品牌配置版本 |
| POST / GET | `/api/v1/sites/{id}/mention-reviews` | 人工提及复核 / 分页审计历史items/next_cursor |
| GET | `/api/v1/sites/{id}/export.csv` | 导出经过统一筛选的事件CSV |
| POST | `/api/v1/sites/{id}/retention-preview`、`retention-execute` | 预览 / 确认删除过期数据 |
| POST / GET | `/api/v1/sites/{id}/sample-jobs` | 创建 / 查看本地模拟采样任务 |

报告/明细支持 `?start=2026-10-28T16:00:00Z&end=2026-10-29T16:00:00Z`（北京时间 10 月 29 日，左闭右开）。省略则查询全部时间，但受容量限制。`+08:00` 用作查询参数时必须 URL 编码加号。错误：400 输入非法，401 未授权，404 不存在，409 重复站点/冲突事件，413 请求体超限，422 查询/响应容量超限，503 数据库忙锁、查询截止或状态检查不可用（按code区分）。

创建站点请求：`{"name":"星舟","url":"https://example.com","brands":["星舟","竞品甲"]}`。
导入请求：`{"adapter":"json-v1","source":"origin-1","records":[...]}`。日志字段见 [demo](scripts/demo.py)，Nginx 字段见 [适配器](geo/adapters.py)。回答使用 `manual-v1`；`surface` 可选 manual / consumer_ui / model_api，禁止把模型 API 结果标为消费产品搜索结果。

`event_id` 必须在同一站点、事件类型、source 内稳定且唯一；同内容重试返回 duplicates，改内容重用 ID 返回 409 并回滚整批。Nginx 适配器接收结构化 JSON（数值 status，绝对 URL），不直接解析 combined 文本或 tail 日志。采集器可用文件代次+偏移生成 ID。

## 范围与下一步

默认仅监听 127.0.0.1；这是本地验证服务，未完成公网生产加固、多租户、常驻调度和真实平台调用。站点、事件及审计历史已支持分页，报告/导出在容量范围内读取完整匹配集合，超限拒绝。GEO_API_TOKEN 为实例管理权限；采集器使用单站点 GEO_INGEST_TOKEN。登记站点并不证明域名所有权，须另完成 HTTPS 挑战。回答正文可能含敏感信息，导入前脱敏；data/ 目录不得提交公开仓库。旧数据库自动升级到 schema v8，升级前建议备份。

爬虫 UA 声明不是已验证身份，抓取不等于收录/引用。没有页面清单时覆盖率为 null；没有回答样本时引用率为 null。品牌匹配为字面匹配，不判断情感、语义或推荐位次。

[架构与参考项目评估](docs/ARCHITECTURE.md) · [第一阶段任务清单](docs/PHASE1.md) · [许可证审查记录](docs/THIRD_PARTY_REVIEW.md)

[站点验证与日志采集指南](docs/INGESTION.md) · [本轮缺陷复查](docs/BUG_REVIEW.md)

[爬虫证据目录与页面覆盖](docs/COVERAGE_AND_CRAWLERS.md)：报告可传 inventory_id 固定快照分母；全新演示站点观察覆盖率2/3、成功覆盖率1/3。

[品牌、人工复核与筛选导出](docs/BRANDS_AND_REPORTS.md)：支持platform、surface、model、source和kind筛选，以及brand_config_id固定匹配定义。

[分页、保留删除、备份恢复与模拟任务](docs/OPERATIONS.md)：当前schema v8；events默认100条，最多500条，需按next_cursor翻页。

[容量控制与运行状态](docs/CAPACITY_AND_STATUS.md)：报告/CSV默认最多10,000条匹配事件，原始事件读取和GET响应分别最多16 MiB；超限返回422且不截断。按日志筛选时回答指标为null，按回答筛选时爬虫指标为null。

[审计分页、删除预算与并发验收](docs/AUDIT_AND_RETENTION.md)：复核历史GET由数组改为分页对象；删除选择合计事件、任务与复核记录，超限不保存残缺计划。运行中缺失数据库不会被普通连接意外重建。`python scripts/local_load.py` 可重复运行临时合成数据并发基线。

[站点分页、清单预算与数据库超时](docs/SITES_AND_DATABASE.md)：站点GET由数组改为分页对象，demo已适配。清单读取先检查存储字节；忙锁503可延迟重试，查询超时503应先缩小查询范围。`python scripts/http_load.py` 提供本地HTTP并发基线。
# 人工样本准备

`python -m geo.samples examples/manual-samples.jsonl` 可离线校验人工回答JSONL，输出逐行错误、警告及汇总，不联网、不写库。[样本格式与证据矩阵](docs/MANUAL_SAMPLES.md)说明填写要求与容量限制；示例均为合成内容。
本地导入支持先预览、后明确提交，跨批次整文件原子入库，结果按计划ID查询。见 [导入流程与回执](docs/SAMPLE_IMPORTS.md)。当前数据库升级到schema v7；历史章节中的v6表示站点分页迁移阶段。预览仅保留计划元数据，但会在事务内模拟写入后回滚样本。
导入计划支持分页查询、过期预览的确认清理及文件质量快照，见 [导入历史与质量口径](docs/IMPORT_HISTORY.md)。当前schema v8；已完成回执保留用于幂等重试，清理不删除业务样本。
