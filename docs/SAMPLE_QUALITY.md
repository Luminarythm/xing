# 站点样本质量报告

2026-10-30；schema保持v8，所有验收数据为本地合成样本。

## 查询

管理员 `GET /api/v1/sites/{id}/quality` 返回当前保留回答的质量计数。支持start、end、platform、surface、model、source、kind与dataset；时间窗左闭右开，带时区，UTC规范化。kind省略或answers，logs拒绝400；不接受brand_config_id或inventory_id，因为质量与品牌匹配/页面清单无关。重复、未知、空参数拒绝。

例：`/api/v1/sites/SITE_ID/quality?dataset=synthetic&start=2026-10-30T00:00:00Z&end=2026-10-31T00:00:00Z`。正时区的加号需URL编码。除dataset外，筛选复用现有事件/报告的校验及SQL谓词；首尾空格统一去除。

dataset默认all，可选synthetic或unclassified。synthetic仅指platform去空格、不区分ASCII大小写后等于synthetic。来源名称、prompt或answer含“合成”不用于推断身份；unclassified并不表示真实或已核验。不存在dataset=real。接口同时返回synthetic_samples与unclassified_samples，合计为samples。

## 分母和未知值

scope=current_retained_answers；samples是筛选后实际入库的回答数，同source/event_id重放不会增加样本。每项质量指标包括count、evaluated_samples、unknown_samples和rate；rate=count/evaluated_samples，分母为零时null。

| 指标 | count含义 | 已评价分母 | 未知来源 |
| --- | --- | --- | --- |
| without_citations | citations数组明确为空 | citations为数组的记录 | 旧记录缺字段/null/非数组 |
| without_evidence_url | evidence_url明确null或空字符串 | evidence_url为null或字符串 | 旧记录缺字段或其他类型 |
| unknown_model | model缺失、空或去空格/不区分大小写等于unknown | 所有选中回答 | 此指标本身计量未知模型，unknown_samples为0 |

例：两条回答中一条citations=[]、另一条缺citations字段，返回count=1、evaluated_samples=1、unknown_samples=1、rate=1。不能将其解释为全部样本都没有引用。空集合count=0但rate=null。by_surface含manual、consumer_ui、model_api与unknown，覆盖全部分母。latest_observed_at仅来自所选样本。

链接存在不证明可访问、可信或正确，也不等于引用本站；后者继续看基础report。没有引用不代表无效回答，质量指标不组成GEO综合评分。

## 与导入摘要对照

导入quality是当时文件快照，分母含重复记录；本站quality是当前库中去重且未删除的回答。对一次无重复、无并发变化的导入，两者计数应相等；后续重放不增库内分母，删除会改变当前报告而不重写历史快照。旧计划未保存quality仍是null。

未知模型占位值现在统一按大小写不敏感处理：UNKNOWN和unknown都计入未知；离线警告、新导入摘要和站点报告共享判断函数。不会重写模型字段或历史导入计划，因此修复前生成的摘要可能与当前报告不同。

## 查询与容量

数据库先筛选，再投影平台、入口、模型、时间、引用数量和证据字段是否存在；不把回答、prompt、引用URL或事件ID传回报告层。只读取投影后的元数据，不依赖品牌复核或清单。query_events限制匹配行数，query_bytes限制投影JSON字节，GET输出仍受response_bytes限制；超限422，不返回截断统计。SQLite解析原始payload仍耗费时间，受现有数据库查询截止约束，不能把投影预算解释为整个进程内存上限。

报告来自单次SQL读取，但与分别请求的事件/基础报告/导入结果不共享跨请求数据库快照；对照时应固定参数并避免并发写入。只允许实例管理员访问，站点日志密钥无权限。

## 验收与下一阶段

新增8项测试覆盖UNKNOWN回归、分母/旧字段未知、合成范围、时区边界及基础报告一致、无需品牌/清单、筛选后容量、导入重放/删除与历史差异、HTTP鉴权和参数拒绝。

下一阶段可增加固定时间窗的本地报告导出包，包含质量口径、基础报告与参数清单；避免把导出文件当作持续变化的实时页面。真实站点/平台和付费provider仍未接入。
