# 参考项目与许可证审查记录

核查日期：2026-10-29。审查范围为以下官方文件，分支可变，首次实际引入代码时必须补充准确 commit SHA 和逐文件清单。

| 项目 | 官方证据 | 本次结论 |
| --- | --- | --- |
| GEOInflow main，README 自述 v1.2 / build 1.2.0 | [README](https://github.com/yaojingang/GEOInflow/blob/main/README.md)、[架构](https://github.com/yaojingang/GEOInflow/blob/main/docs/system-overview.md)、[LICENSE](https://github.com/yaojingang/GEOInflow/blob/main/LICENSE) | MIT；版权声明包括 Umami Software (2022) 与 Liekan contributors (2026)，架构自述基于 Umami v3.4.1 |
| Getcito master，README 自述 v9 基于 Elmo | [README](https://github.com/ai-search-guru/getcito-worlds-first-open-source-aio-aeo-or-geo-tool/blob/master/README.md)、[LICENSE.md](https://github.com/ai-search-guru/getcito-worlds-first-open-source-aio-aeo-or-geo-tool/blob/master/LICENSE.md)、[packages](https://github.com/ai-search-guru/getcito-worlds-first-open-source-aio-aeo-or-geo-tool/tree/master/packages) | MIT；保留 Blue Whale Software, LLC 与 GetCito (2026) 声明；GitHub 元数据为 Other/NOASSERTION，不能用该标签替代原文判断 |

## 借鉴与复用边界

本次只参考模块职责与产品能力，xing 源码独立编写；没有引入上游代码或第三方运行依赖。尚未为 xing 自动选择对外开源许可证，仓库公开不等于授权他人任意使用；发布复用授权前由维护者决定 LICENSE。

如果后续复制 MIT 代码或实质部分，需保留完整许可证与相应版权声明，并记录文件、版本、修改说明。不能只写“来源 GitHub”或仅保留 GetCito/GEOInflow 的名称而删除其上游版权。MIT 不代表品牌、商标、第三方素材或托管服务一并授权。

GEOInflow README 明确第三方依赖、字体、地理数据有各自条款；其中 DB-IP City Lite 为 CC BY 4.0 并有署名要求，外部 MaxMind 数据另有条件。当前没有引入这些数据；复制完整镜像或数据包前需逐项检查 THIRD_PARTY_NOTICES 和实际文件。

Getcito README 提到 CLA，这是向其上游贡献的规则，不能混为 xing 自有代码许可或消费者必须签署的条件。其 BrightData/Oxylabs 等采集服务、Auth0 与其他模型供应商有独立凭证、成本和使用条款；MIT 不授予免费服务额度或绕过平台限制的权利。

## 实际代码引入前的完成条件

1. 固定上游 commit SHA、来源文件和本地目标路径；记录下载时间与修改摘要。
2. 查该版本 LICENSE、文件头、NOTICE、素材与依赖锁文件；把所需完整声明随代码分发。
3. 列依赖清单，核查传递依赖和分发方式；出现未知许可先隔离，避免将不明内容纳入发行包。
4. 排除私钥、客户日志、默认示例中的可识别信息、商标和无授权数据集。
5. 对国内采集源另行确认接口权限、数据处理范围、保留时间与删除能力；开源许可不能代替数据来源授权。

这是一份工程复用风险记录，并非对所有版本、服务条款或商业场景的全面法律审计。
