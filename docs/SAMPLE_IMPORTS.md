# 本地样本导入预览与结果记录

2026-10-30；仅合成样本验收。schema v7新增sample_imports，保存计划和结果元数据，不保存输入文件路径、prompt、answer、引用或事件ID。来源名称source会保存，因此不要把个人信息放入source。备份/恢复和状态检查同步支持v7。

## 使用

先启动应用创建本地库和站点，并用 `python -m geo.samples examples/manual-samples.jsonl` 检查文件。导入CLI要求数据库文件已存在，使用本机文件权限，不通过HTTP、不要求API token；启动时会迁移旧库，先备份，勿与旧版程序混用。

```powershell
python -m geo.imports preview --db data/geo.sqlite3 --site SITE_ID --file examples/manual-samples.jsonl
python -m geo.imports execute --db data/geo.sqlite3 --site SITE_ID --file examples/manual-samples.jsonl --plan PLAN_ID --confirm import-previewed-samples
python -m geo.imports result --db data/geo.sqlite3 --site SITE_ID --plan PLAN_ID
```

SITE_ID来自站点注册，PLAN_ID来自preview结果。CLI退出码0表示命令完成（预览仍需检查executable），1表示操作失败；参数错误退出2。CLI失败仅给通用错误，不泄漏SQL、文件路径或正文。执行前用预览和result定位批次状态，必要时重新预览。Python调用接口会给具体的Conflict/ValueError等异常。

## 预览契约

读取一次有界输入，复用离线校验（8 MiB、10,000行、单行256 KiB）；随后只解析同一份已校验字节，防止校验后重新打开文件造成竞态。按去空格后的source分组，每批最多500条，保留同来源的文件顺序。完整输入行数还受query_events约束。Unicode行分隔符出现在JSON字符串内不会被误认为物理换行。

预览在BEGIN IMMEDIATE事务与保存点内调用实际入库逻辑，得到真实的库内重复、冲突及保留边界检查结果，然后回滚全部样本和事件序号变化，仅保存计划。**这不是只读连接**：模拟写入会占用写锁和产生临时SQLite日志；不保证敏感正文从磁盘物理擦除。只有此前的geo.samples离线校验完全不访问数据库。

计划包含文件SHA-256、site_id、品牌配置版本、保留截止、15分钟有效期、各批次source/条数/预计新增/重复或conflict/rejected状态。任一批次不ready时executable=false；不会自动跳过错误样本。计划JSON受query_bytes与response_bytes较小值限制，超限不保存计划。校验失败的文件也不保存预览。

## 执行与结果

必须传精确确认字符串。重新读取并校验文件，摘要必须完全相同（即使只改空白也需新预览）；站点、配置与保留边界必须匹配。执行在一个写事务内重新运行所有批次，结果必须与预览相同。全部匹配才提交样本和committed结果，包含inserted、duplicates、committed_at、attempts；整个文件原子提交，没有“前几批成功后几批失败”的残留。

数据库记录变化导致新增/重复/冲突统计改变时，回滚样本、保存last_attempt的批次状态并报Conflict，要求重新预览。文件变化、跨站、过期、配置/保留边界变化在执行前拒绝；数据库忙锁、查询超时、进程中断等运行失败整笔回滚，可能无法保存失败记录。此时result仍显示上一次持久状态，不能把attempts当作所有尝试的完整日志。

同一已完成计划与同一输入文件的重试直接返回保存结果，不再次入库。后来通过保留策略删除样本，也不会被完成态重试复活。此结果是历史回执，不代表样本当前仍存在。新计划重新导入仍遵循现有保留边界。

提交结果序列化/保存失败也会回滚样本。预算调低可导致旧计划无法读取或执行；不会为适应预算静默截断。来源分组只是处理单元，不提供独立批次提交。文件、来源、配置或数据集合变化后应新建预览。

## 缺陷复查与限制

修复注册时Go与全角Ｇｏ等规范化重名被接受的问题：注册与品牌配置采用相同NFKC/casefold规则。原有完全相同名称的去重行为保留；旧库的重名品牌不自动改写，可用品牌配置修正。

11项新增回归覆盖501条跨批事务、重复与回执重放、预览后文件变化、跨站/过期/配置变化、晚到冲突、注入中断、保留删除后不复活、容量/非法文件、Unicode物理行、旧库迁移与备份恢复、无网络CLI。所有测试用临时合成库。

仍无真实平台采集、前端导入界面、远程导入API或后台任务。计划按ID读取，尚无历史列表/计划清理命令。下一步可补只含元数据的计划分页与清理，以及导入后的样本质量摘要；真实样本验收继续待办。
