# 本地双时间窗对照

```powershell
python -m geo.bundle_compare data/before.zip data/after.zip
python -X utf8 -m geo.bundle_compare data/before.zip data/after.zip --summary
```

两个参数分别是前期、后期报告包。先按REPORT_BUNDLES.md导出，必须采用相同站点及相同筛选。该命令复用离线校验器，读取已有包，不连接数据库、不联网、不解压或修改文件。默认输出JSON；`--summary`输出简短中文文本。校验失败、跨站、筛选不一致、逆序、重叠或同一窗口对照均退出1且不输出部分结果。

前期end必须小于或等于后期start，按UTC实际时间比较。允许中间有空档、窗口时长不同，但分别输出gap_between_windows、unequal_window_duration提示。没有按天折算计数，不支持把同一窗口两次导出当作趋势对照。筛选采取规范化后的精确字典相等，`{}`和`{"kind":"answers"}`视为不同口径。

JSON包含两期窗口、快照生成时间、配置/清单ID、样本数、显式合成和未分类数量，以及基础指标、质量指标、品牌分组和口径提示。差值方向为后期减前期；比例差乘100，单位percentage_points。例如50%降至25%输出-25个百分点，不是-50%相对变化。计数只描述上传并保留的样本。`comparable=true`仅代表该差值未因本模块规则暂停，不表示两期具有统计代表性或可归因。

| 情况 | 处理 |
| --- | --- |
| 任一数值为null | delta=null，reason=unknown_value，不当作零 |
| 品牌配置ID、定义内容或匹配规则不同 | 品牌提及总数/各品牌差值暂停，保留两期值；reason=brand_configuration_changed |
| 清单ID、大小或包内可见URL集合不同 | 两项覆盖率差值暂停；reason=inventory_changed |
| 回答分母不同 | 保留各期分母与差值，提示sample_denominator_changed |
| 质量指标可评估分母不同 | 每项输出denominator_changed及各自count/evaluated_samples/unknown_samples/rate |
| 日志被回答筛选排除 | 日志指标与覆盖率保持未知，差值不计算 |

配置变化原因优先于unknown_value；新增/移除品牌在缺失一侧显示null，绝不自动补零。配置或清单ID不同，即使内容恰好相同也保守暂停对应差值；ID相同但可见定义不同也暂停。人工复核决策不在报告包中逐条导出，即使配置ID未变，两期仍可能采用不同复核结果。独立导出的两包不是跨窗口数据库共同快照，补录与保留删除也可能改变结果。报告校验与配置一致都不证明真实来源。

每包读取/解压及对照JSON输出均使用本地GEO_MAX_RESPONSE_BYTES（默认16MiB），该值是分别的容量上限，不是两包合计内存限制；两个已解析结果会同时占内存。输出不带原始回答或文件路径。中文摘要只展示选定指标，完整质量分母、品牌分组及限制见JSON。数据字段以JSON转义输出，无法执行包内脚本。

验证：`python -m unittest tests.test_bundle_compare -v`。9项测试覆盖百分点方向、分母变化、空值与零、配置/清单改变、同ID不同定义、缺失必要元数据、跨站/筛选、重叠/逆序、间隔/不等时长、容量拒绝不改包和CLI失败无部分输出。

后续可执行任务：在保留上述口径规则的基础上补充中文摘要中的质量分母与品牌细项，再评估人工指定同内容配置是否应被视为可比；在规则确定前不放宽当前保守判定。
