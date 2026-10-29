# 品牌实体、人工复核、筛选与 CSV 导出

阶段 P05/P07 的本地实现，schema v4。无需模型调用，不接真实站点。数据库从旧版本增量建表，不改写历史回答。

## 统一筛选

`GET /api/v1/sites/{id}/report`、`/events` 和 `/export.csv` 共用以下参数：

| 参数 | 规则 |
| --- | --- |
| start / end | 带时区ISO时间，UTC左闭右开 |
| kind | logs 或 answers |
| platform / model | 与回答字段精确匹配，区分大小写 |
| surface | manual / consumer_ui / model_api |
| source | 精确匹配采集来源 |
| brand_config_id | 固定品牌配置版本；省略采用最新版本，legacy指原始站点品牌列表 |

report另支持inventory_id；它只控制爬虫覆盖分母，不适用于events/CSV。未知参数、重复参数、空值和非法枚举返回400；例如拼错platfrom不再返回未筛选结果。kind=logs与回答专属筛选不能组合。

示例：`/api/v1/sites/{id}/report?platform=Kimi&surface=manual&source=fixture`。对应的events和export.csv使用相同参数，事件行数对应报告回答样本数；无匹配时返回空集，比例为null。回答专属筛选会排除全部日志，report.log_scope为excluded_by_answer_filter，爬虫请求及覆盖率为null，不能解释为爬虫没来过。统计还提供按平台、入口的样本计数。

筛选和导出均为当前数据库的实时查询，并非跨多个HTTP请求的原子快照；新事件或人工修正可能改变后续查询结果。需要可比时应固定时间、配置和清单编号，且在导出期间停止写入。分页、异步大数据导出和完整报告快照尚未实现。

## 品牌与竞品

管理员 `GET /api/v1/sites/{id}/brands` 获取当前配置，返回配置id和每个品牌的稳定id。旧站点品牌列表自动提供legacy配置，role=unspecified，不猜测哪个是竞品。

管理员 `POST /api/v1/sites/{id}/brands` 提交完整配置，创建不可变新版本：

```json
{"brands":[{"name":"星舟","role":"own","aliases":["StarShip","星舟产品"]},{"name":"竞品甲","role":"competitor","aliases":["OtherBrand"]}]}
```

品牌最多50个，每个品牌最多50个别名。品牌名经Unicode NFKC和大小写规范化后不能重复。编辑已有品牌必须保留GET返回的品牌id；新增品牌省略id。不能使用其他站点或当前配置不存在的id。删除表示从新配置省略，旧配置保留；恢复已移除品牌需要新建id。角色仅own、competitor、unspecified。

匹配规则alias-boundary-v1：Unicode NFKC与casefold后匹配；ASCII别名必须有字母/数字/下划线边界，Go不再匹配Google；中文仍采用字面包含，不宣称语义理解。一个回答中多个别名命中同一品牌只计一个样本。共享别名只有歧义命中时不自动计入任一品牌，status=ambiguous；若某品牌另有唯一别名命中，则该品牌可计入。否定句仍算提及，不算正向推荐或情感判断。

report和events使用选定配置重新投影历史回答。events保留original_mentions，原始数据库事件不改；brand_config_id、mention_rule、mention_details标明本次计算口径。报告按所选配置名称输出brand_mentions，品牌稳定id与角色见brand_definitions。固定旧brand_config_id可以重现旧别名定义，但该配置后续人工复核仍会影响结果。

## 人工修正与审计

管理员 `POST /api/v1/sites/{id}/mention-reviews`：

```json
{"config_id":"配置id或legacy","brand_id":"该配置中的品牌id","source":"manual-import","event_id":"answer-001","included":true,"reason":"根据回答上下文确认此处属于星舟品牌"}
```

校验本站真实存在的回答、配置和品牌；included必须是布尔值，理由必填。每次新增审计记录（id、actor=instance_admin、时间、理由），不覆盖旧记录；同一配置/来源/事件/品牌以最后提交的复核为准。取消误判可再提交included=false；重新确认可再提交true。原回答与原始自动提及不修改。

`GET /api/v1/sites/{id}/mention-reviews?brand_config_id=...` 读取完整审计历史。复核只影响指定配置，换新配置不会自动继承旧复核，避免别名变化造成错误套用。目前没有撤销至“全自动”的独立操作；人工可提交新的明确判断，或采用新配置重新评估。单运营者实例尚不能区分多个真实审核人。

## CSV

`GET /api/v1/sites/{id}/export.csv` 输出UTF-8 BOM、固定文件名xing-events.csv。列包含事件类型、来源、编号、时间、平台、模型、入口、URL、状态码、提示词、回答、引用、提及与品牌配置编号。列表/对象以JSON存入单元格，csv模块负责逗号、双引号和换行转义。

可能触发电子表格公式的单元格（=、+、-、@前缀，含前导空白；或制表/回车/换行开头）加单引号。该处理仅影响导出显示，JSON证据不改。导入其他程序时应注意这个前缀；CSV不作为无损备份替代品。所有导出和品牌/复核接口都需要管理员token，站点采集密钥仅能写日志。

## 验证与待办

本地回归覆盖英文短名误匹配、Unicode重名、共享别名、配置历史、跨站隔离、复核历史、重复导入、报告/明细/CSV一致、非法筛选、CSV公式与多行中文。真实中文同名消歧需要人工样本继续验证；尚未实现语义模型、推荐位次、情感、批量复核界面或外部平台采样。
