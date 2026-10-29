# 爬虫证据目录与页面清单覆盖

核查日期：2026-10-29；目录版本3；数据库 schema v3。只进行本地合成数据验收；公开文档查询不等于接入或抓取真实业务站点。

## 目录与证据分级

管理员 `GET /api/v1/crawlers` 可读取目录与待核实项。每条启用规则包含 provider、category、source_url、evidence_level、reviewed_at、rule_version 和 verification_method。

| 标识 | 分类 | 本轮核实来源 |
| --- | --- | --- |
| GPTBot、ClaudeBot | ai_crawler | [Cloudflare 官方爬虫目录](https://developers.cloudflare.com/ai-crawl-control/reference/bots/) |
| OAI-SearchBot、PerplexityBot | ai_search | 同上 |
| ChatGPT-User | ai_assistant | 同上 |
| Bytespider | ai_crawler，ByteDance | 同上；属于基础设施厂商文档证据，不标为字节官方声明 |
| Baiduspider | search_engine，Baidu | [百度官方帮助](https://www.baidu.com/search/robots_english.html)；传统搜索单列，不算AI请求 |

分类来自明确文档；目录并未验证任何一条请求的来源。UA 匹配仍为 unverified；没有可信IP/DNS/边缘证明，verified_ai_requests 保持0。Bytespider 请求不等于豆包引用或曝光。

Kimi、豆包产品专用标识、DeepSeek、通义、腾讯混元保留 pending_primary_evidence 且 enabled=false。本轮没有充分核实它们的运营者官方爬虫说明，不依据品牌名生成启用规则。检索过的 [Kimi /bot](https://www.kimi.com/bot) 返回产品页面，[豆包 /bot](https://www.doubao.com/bot) 未呈现可读取的爬虫说明；这不证明相关说明不存在，只表示此次核查未建立足够证据。后续需补充明确的标识、用途、来源和核验方式。

UA 中同时出现多个启用标识时记录 ambiguous_tokens，保留未知类别，不按目录顺序任选一个。目录版本写入每条新日志；历史事件保持原分类，不靠新版名单回填“已验证”。v1/v2 的五个既有AI标识按其当时含义继续计数，重传不因新版派生字段增加而冲突。未知/空UA保留请求但不算AI。

新增规则需修改 geo/catalog.py，附来源、日期和版本并跑边界测试；本轮无远程动态更新，也无管理员运行时任意启用接口。P03目录基础已实现，国内运营者证据全覆盖及实际IP/DNS核验仍待办。

## 导入页面清单

管理员调用 `POST /api/v1/sites/{id}/inventory`：

```json
{"source":"manual-export-2026-10-29","urls":["https://example.com/a","https://example.com/b"]}
```

每次成功导入创建不可变快照，返回 id、created_at、source、urls。最多10000个输入URL（仍受HTTP总请求2MB限制）；整批验证成功后落库，不会先写部分。仅接受本站相同netloc的绝对HTTP(S)地址，scheme保留；查询串与片段删除后去重。跨站URL拒绝，空数组允许作为明确的空清单。不获取远程 sitemap、不访问清单内URL，当前只支持JSON清单。

`GET /api/v1/sites/{id}/inventory` 返回最新快照，无清单返回null；追加 `?inventory_id=...` 读取指定快照，跨站或不存在返回404。重复POST会新建快照，不追加或修改旧快照。

## 报告口径

`GET /api/v1/sites/{id}/report?inventory_id=...&start=...&end=...`

时间范围仍为左闭右开UTC；不指定inventory_id使用最新快照。做历史比较时必须保存并显式传入该编号。清单是运营者提供的页面集合，不保证完整、当前存在或已被索引。

| 字段 | 含义 |
| --- | --- |
| coverage_rate / coverage.observed_coverage_rate | 时间窗内至少有一条声明AI请求的清单页面数 / 清单总页面数 |
| successful_coverage_rate | 时间窗内至少收到一次2xx的清单页面数 / 清单总页面数 |
| coverage.pages | 每URL的请求数、2xx、>=400失败、3xx重定向及观察/成功布尔值 |
| coverage.outside_inventory_urls | 观察到的声明AI URL中不在所选清单的部分 |
| search_engine_requests | 单列传统搜索请求，不纳入上述AI覆盖分子 |
| crawler_rule_versions | 当前查询范围内使用过的规则版本，便于解释历史口径 |

缺失清单或空清单：两种覆盖率均为null。非空清单但无观察日志：覆盖率为0，仅说明未在该数据范围观察到请求。2xx只是HTTP响应成功，不证明正文完整；3xx/304不计成功；出现过失败但后来2xx的URL可以同时贡献失败请求和成功页面。报告不推断收录、训练、引用或曝光。

精确匹配规范URL（含scheme和path），不合并HTTP/HTTPS、尾斜杠、路径大小写或百分号编码等价项；query维度被刻意丢弃。清单和日志均采用相同规则。报告尚未分页或预聚合，适用于本地MVP规模。

## 本地示例与验收

启动服务后运行 `python scripts/demo.py`。新演示站点创建3页清单，合成日志访问其中2页，1页收到2xx、1页404：观察覆盖率2/3，成功覆盖率1/3；其余旧演示计数不变。旧演示站点已有清单时保留该清单。

`python -m unittest discover -s tests -v` 覆盖缺失/空清单、重复URL、失败/重定向、清单外URL、时间边界、历史快照、跨站隔离、HTTP权限、旧目录事件重放、模糊UA以及空UA采集链路。真实站点与连续七日验收仍未进行。
