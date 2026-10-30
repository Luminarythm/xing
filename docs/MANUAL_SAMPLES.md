# 人工样本与离线校验

2026-10-30，P04本地准备。schema仍为v6，没有真实平台调用或真实样本验收。

## 可执行流程

复制 examples/manual-samples.jsonl 到本机数据目录，按每个回答一行JSON填写。样例全部是合成内容，example域名不是有效证据。运行：

```powershell
python -m geo.samples examples/manual-samples.jsonl
```

命令只读取输入文件，不打开数据库、不联网、不写入样本。标准输出是JSON诊断：行号、固定错误类别和警告，以及checked_rows、valid_rows、invalid_rows、duplicate_rows、complete。退出码0为格式通过，1为内容/容量校验失败，2为文件无法读取。没有任何写库或自动导入副作用。需要保存诊断可由调用方重定向标准输出。

## JSONL格式

每行必须恰有source与record两个字段。record必填字段：

| 字段 | 填写要求 |
| --- | --- |
| event_id | 同站点、同source内稳定唯一，最长200字符；重试保留原编号 |
| observed_at | 采样时刻，带时区；示例2026-10-30T09:00:00+08:00 |
| platform | 实际产品或采集平台名称；合成样例统一synthetic |
| model | 实际显示的模型，无法确认填unknown，不自行推断 |
| surface | manual / consumer_ui / model_api；不能把模型API标成消费产品入口 |
| locale | 采样语言/地区标签，例如zh-CN |
| prompt | 完整问题，最长5000字符，JSON内换行写为\n |
| answer | 回答原文，最长50000字符；没有品牌或引用的回答也保留 |
| citations | URL数组；没有引用必须填写[]，最多100个 |
| evidence_url（可选） | 可复核证据的HTTP(S)链接，或null；不是引用列表的替代品 |

source最长100字符，建议同一采样批次使用稳定来源标识，合成样例使用synthetic-training。JSONL逐行包装不是HTTP导入请求；正式入库需按source分组，将record装入现有 `{adapter:"manual-v1",source,records:[...]}`，每批最多1000条。本轮不提供自动导入。校验器不读取目标站点，因此不能检测库内重复、保留边界或其他写入时冲突。

严格离线模板比兼容旧调用的API更严格：要求显式填写model、surface、locale、citations，拒绝未知字段和重复JSON键。使用与API相同的ManualAnswerAdapter校验类型、时间和URL。文件内对去除首尾空格后的(source,event_id)检查重复，后续重复行即使内容相同也报错；不同source可重用event_id。校验器不是去重写入器。

证据链接缺失、模型unknown是警告，不意味着格式失败；格式通过也不意味着证据真实。API会移除URL查询串与fragment，离线校验在遇到这种情况时给出警告。有些分享链接依赖查询串，规范化后可能无法复核，采样者需另行保留受控证据；本轮没有改变既有URL隐私处理策略。

## 容量与诊断

UTF-8（可带文件首BOM），最多8 MiB、10,000条物理行，每行含换行最多256 KiB；多行回答须JSON转义。空行不是样本，会报无效JSON。遇到容量上限立即停止，complete=false、fatal=input_limit_exceeded，checked_rows仅表示已经检查的前缀，绝不能把前缀通过当成整文件通过。空文件失败。

固定错误类别：invalid_json_or_utf8、invalid_envelope、missing_required_fields、unknown_fields、invalid_record、duplicate_identity。错误和警告不回显正文、prompt、来源标识、URL或event_id；敏感内容留在原文件。每行给出结构问题或适配器校验结果，不保证一次列出所有字段错误。

## 入口与证据要求矩阵

以下是本项目的记录契约，不是供应商当前功能或接口可用性的声明。

| 入口 | 本项目可记录 | 需要保留的证据 | 当前接入状态 |
| --- | --- | --- | --- |
| manual | 人工整理的回答、引用与品牌事实 | 原始回答出处、时间、原文和引用；来源不明不可解释为平台曝光 | 本地适配器已实现 |
| consumer_ui | 人工观察到的消费产品回答 | 产品入口、显示模型、时间、问题、原文、可复核链接；无法确认模型记unknown | 数据格式支持，未实采 |
| model_api | 经授权取得的模型接口响应 | 实际模型、调用时间、原始响应与返回引用；不等同消费产品结果 | 格式支持，真实provider未接入 |

| 待验收平台 | 消费入口样本 | 官方API能力/权限 | 本项目真实采集 |
| --- | --- | --- | --- |
| Kimi | 未验证 | 未验证 | 未接入 |
| 豆包 | 未验证 | 未验证 | 未接入 |
| DeepSeek | 未验证 | 未验证 | 未接入 |
| 通义 | 未验证 | 未验证 | 未接入 |
| 腾讯相关产品（需明确产品名） | 未验证 | 未验证 | 未接入 |

实际采样前另行核对官方文档、运营者授权和证据保存方式。本表没有查证这些平台的当前能力，不把字段可填写视为平台已集成。当前用户选择仅本地验证，真实两平台各10条样本与连续采集退出条件保持未完成。

## 缺陷与验收

修复evidence_url的false、0、[]、{}被静默丢弃的问题；现在拒绝这些类型，null、字段缺失和旧版空字符串仍可表示未提供。9项新增测试覆盖共享适配器、字段/URL/入口、重复编号与JSON键、UTF-8/BOM、容量精确边界、诊断不泄漏、无网络CLI及合成示例。

下一步：加入仅面向本地服务的预览式导入流程，逐批显示数量、来源和冲突，保持幂等且记录批次结果；真实站点与付费provider继续待授权。
后续本地导入已实现：geo.imports提供preview/execute/result，使用本文JSONL格式。geo.samples仍保持纯离线、只读文件；导入预览会访问并写入计划数据库。最新契约见 SAMPLE_IMPORTS.md，历史“尚无自动导入”描述仅对应本阶段初始版本。
