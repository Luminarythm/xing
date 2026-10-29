# 本地运维：分页、保留删除、备份恢复与模拟任务

当前schema v6，站点索引迁移与数据库超时契约见 SITES_AND_DATABASE.md；仅本地合成数据验证。新增功能不代表已具备公网生产能力。未设置自动删除计划或操作真实业务数据。

## 事件分页（接口格式变更）

`GET /api/v1/sites/{id}/events?limit=100` 现在返回对象，而非旧版数组：

```json
{"items":[],"next_cursor":null,"limit":100,"brand_config_id":"legacy","snapshot_high_watermark":0,"review_high_watermark":0,"order":"ingestion_sequence"}
```

limit为1–500，默认100。下一页保持同一站点、筛选、时间及brand_config_id参数，带上URL编码后的cursor。游标有效1小时，签名绑定实例管理员token；token轮换后旧游标失效。篡改、跨站、改变筛选和过期返回400。

排序是入库序号，不是事件时间。首屏固定事件序号上限、品牌配置和复核序号上限；后来的事件、配置修改和人工复核不会混入本次遍历。永久递增序号不因删除末行而复用。保留删除优先于分页快照，已删除记录会从后续页消失，不保留可读副本。

SQL先筛选再取limit+1条，避免深OFFSET和全量事件加载。品牌投影只读取所选回答的最新复核，但数据库仍可能扫描较多审计记录，尚未做大规模性能压测。report和export.csv在容量范围内读取完整匹配集合，超限422；未做流式下载、分页聚合或跨请求原子报告快照，具体预算见 CAPACITY_AND_STATUS.md。CSV与报告的完整统计需与全部分页items的合并结果比较，不能仅比较首屏。

## 保留期限与精确删除预览

管理员先调用 `POST /api/v1/sites/{id}/retention-preview`：

```json
{"before":"2026-10-01T00:00:00Z"}
```

返回预览id、before、count、相关job_ids和review_sequences、expires及摘要。仅选择本站observed_at严格早于before的事件，边界时刻保留。预览15分钟有效，不删数据。先检查事件数、关联任务/复核数量及截止时间，再调用 `POST /api/v1/sites/{id}/retention-execute`：

```json
{"plan_id":"预览id","confirm":"delete-previewed-events"}
```

执行前在写事务中核对事件集合与关联任务/复核是否变化；有新旧日期事件、任务或复核变化时返回409，要求重新预览。过期或缺少明确confirm返回400，跨站预览返回404。成功删除后重复执行同一个预览返回原结果，不再次删除。

删除包含选定事件、对应回答的所有人工复核，以及本站早于截止时间的模拟任务（包括待执行/执行中任务）。任务租约与写入事务保护阻止已删除任务继续回写。站点、品牌配置、页面清单、密钥和仅含元数据的删除记录保留。

成功后写入本站永久保留时间边界，旧采集重传和旧日期任务不能重新入库；边界只前移，不后退。**设置未来截止时间也会阻止该时刻之前的新导入**，必须在预览阶段核对。当前没有解除边界接口。采集器收到过期事件拒绝时应人工处理旧outbox，不能盲目重试或删除checkpoint。

这是数据库逻辑删除，不承诺SQLite空闲页的物理擦除。备份、导出文件、采集器outbox不会自动删除；它们需由运营者另行管理。真实生产前还需保留策略配置、容量/批次限制、加密与物理删除流程。

## 备份与恢复（仅本地命令）

```powershell
python -m geo.backup backup data/geo.sqlite3 data/backup-20261029.sqlite3
python -m geo.backup restore data/backup-20261029.sqlite3 data/restored.sqlite3
```

使用SQLite在线backup接口，检查数据库完整性、版本、必需表与外键一致性；目标文件必须不存在，拒绝覆盖原库或任何已有文件。失败只清理本次新建的输出文件。不通过HTTP暴露服务器文件路径访问。

恢复产生新文件，不自动切换运行服务。核对新库报告后，停服务、停worker，再把GEO_DB指向恢复文件并启动；旧版本库由应用启动时迁移。恢复到备份时点会恢复当时的密钥/撤销状态、待执行任务和保留边界，也可能恢复后来被删除的数据。切换前应核对并重新执行必要的撤销与删除；不要让两个恢复副本同时对同一业务采集源运行。备份含全部业务内容，不能提交到公开Git。

## 持久模拟采样任务

管理员 `POST /api/v1/sites/{id}/sample-jobs`：

```json
{"provider":"mock","request_key":"smoke-001","prompt":"本地合成测试问题"}
```

只支持mock，不调用模型、不联网、不计费。可选observed_at用于合成时间测试，默认当前UTC。同站点request_key是幂等键：相同输入重提返回原任务，不同prompt或显式时间返回400。请求会保存prompt，但回答是固定合成文本，platform=synthetic、model=mock-v1、surface=manual，不能视为真实平台表现。

`GET /api/v1/sites/{id}/sample-jobs` 返回最近100个任务元数据，不返回原始prompt。运行一个任务：

```powershell
python -m geo.jobs
```

使用与服务相同GEO_DB。命令每次最多处理一个任务，不安装后台循环或定时器。状态queued/running/succeeded/failed；30秒租约，最多3次尝试。失败按2的尝试次数次方秒延后；失联任务租约到期可重新领取。过期worker无权提交；事件入库与任务完成在同一事务，重复完成不会增加样本。达到上限保留failed，需人工检查后用新request_key创建新任务。

## 权限、验证与剩余范围

分页、删除和任务接口需要实例管理员token，站点日志密钥无权使用。备份恢复依赖本机文件权限。104项本地测试覆盖分页签名/范围、快照上限、删除重试/变更/过期、防止重放复活、任务租约/重试上限、备份恢复、容量边界、状态检查、审计/站点分页、保留计划预算、清单预算、忙锁/超时回滚、缺库不重建及旧版本回归。

P08仅完成本地模拟任务基础，真实provider接入仍待办。P09完成站点/事件/复核分页、带容量预算的显式保留删除、新文件恢复、报告/清单/导出预算、管理员状态摘要与数据库错误契约。服务层基线见 AUDIT_AND_RETENTION.md，HTTP多连接基线见 SITES_AND_DATABASE.md；TLS网关、限流、多租户、自动保留计划、更大规模压力验收、后台巡检与灾备演练仍待办。
