# 导入历史、过期预览清理与质量摘要

2026-10-30；本地合成数据验收。schema v8新增import_positions持久递增序号，旧计划按rowid回填，清理级联删除序号记录但不会复用编号。备份与状态检查同步支持v8。

## 本地命令

使用已有数据库和站点；设置与服务相同的GEO_API_TOKEN（至少24字符）签名游标。命令仍采用本机文件访问权限，该密钥用于防止游标被修改，不形成多租户隔离。命令不联网。

```powershell
python -m geo.import_history list --db data/geo.sqlite3 --site SITE_ID --limit 100
python -m geo.import_history list --db data/geo.sqlite3 --site SITE_ID --cursor NEXT_CURSOR
python -m geo.import_history cleanup-preview --db data/geo.sqlite3 --site SITE_ID --limit 100
python -m geo.import_history cleanup-execute --db data/geo.sqlite3 --site SITE_ID --cursor CLEANUP_CURSOR --confirm delete-expired-import-previews
```

列表返回items、next_cursor、snapshot_high_watermark和limit。默认100，最多500；固定序号上限，排除新增计划，删除项跳过。每项仅包含ID、状态、条数、时刻、新增/重复数和质量计数，不包含source、批次明细、文件摘要或回答正文。**状态为查询时实时值**：遍历期间计划从preview变为committed会在后页反映，不是历史状态快照。游标绑定站点、资源和密钥，有效1小时，密钥轮换失效。读取与输出受已有字节预算约束。

## 清理契约

cleanup-preview选择当前时刻已到期且state=preview的计划，最多limit条，并受query_events较小值限制。返回count、固定cutoff和签名cursor，有效15分钟；不删除也不持久化清理计划。limit可能小于全站过期总数，返回的是本批精确数量，不是总量。后续可重新预览处理下一批。

cleanup-execute要求精确确认字符串。在一个写事务里重新选择固定截止时刻/序号范围内的记录，核对编号与到期时间摘要；集合变化则拒绝，不部分清理。返回结果先检查输出容量再删除。跨站、跨资源、篡改、过期游标或时钟倒退均拒绝。重复执行已成功清理的非空选择会报集合变化，需重新预览；不会误删下一批。没有自动清理计划或后台循环。

只删除过期未提交预览及其序号记录，不删除事件、品牌复核、保留水位、未到期预览或committed回执。完成回执保留，以保证导入重复执行返回历史结果而不重新写入。清理先于提交取得写锁时，提交找不到计划；提交先取得锁时，到期检查拒绝过期计划。不会用清理功能销毁完成态幂等记录。

此删除是SQLite逻辑删除，不承诺物理擦除。CLI成功退出0，错误退出1并给通用代码；没有HTTP历史或清理接口。升级前备份，勿与旧版本程序混写。

## 质量摘要口径

新预览存储quality：scope=input_file_including_duplicates、samples、without_citations、without_evidence_url、unknown_model和by_surface。输入使用已有适配器验证，摘要按文件内容统计；缺少引用不代表无效回答，缺少证据链接不代表事实错误。它不是平台评分，也不是当前数据库总体质量。

摘要覆盖文件中包含的重复记录；即使新增数为0，samples仍是文件样本数。提交后沿用同一快照，后续删除/品牌复核不重算。v7及更早计划没有摘要时列表返回quality=null，不能解释为0。详细批次结果仍通过geo.imports result获取。

## 缺陷与验收

修复导入计划在expires精确时刻仍允许提交的问题：由expires < now改为expires <= now。回归修复前失败、修复后通过。9项新增测试覆盖质量口径、编号不复用、清理保护、确认/签名/站点/过期边界、竞争清理导致集合变化、输出失败前不删除、旧计划质量未知及v7→v8备份恢复、无网络CLI。

下一阶段可实现按站点和时间窗的样本质量报告，明确合成样本范围与质量分母，并复查报告与导入摘要的差异。真实站点和付费provider仍待授权。
