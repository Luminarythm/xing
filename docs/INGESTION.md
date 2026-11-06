# 站点验证与可靠日志接入

本阶段按用户要求仅本地验证，不向真实域名发出请求，不导入真实站点数据。自动测试使用可注入的验证响应；正式服务没有“跳过验证”开关。仍是单运营者、本机 API，未完成公网生产加固。

## P01：站点所有权与采集密钥

管理员 token 调用以下 POST 接口，前缀 `/api/v1/sites/{site_id}/`：

| 接口 | JSON 请求 | 结果 |
| --- | --- | --- |
| ownership-challenge | `{}` | url、content、expires_at；挑战有效期30分钟 |
| ownership-verify | `{}` | 服务读取 HTTPS 静态文件，精确核对 content；验证有效30天 |
| ingest-keys | `{}` | 仅已验证站点可签发；返回 id 和一次性显示的 token |
| revoke-key | `{"key_id":"已签发的id"}` | 立即撤销；不存在返回404 |

将挑战 content 作为纯文本放在返回 URL 对应位置。只支持 HTTPS 443 的 `/.well-known/xing-verification.txt`，DNS TXT 未实现。验证拒绝内网、回环、组播、保留地址和含这些地址的混合 DNS 答案；连接固定已验证 IP，TLS 使用原始域名验证证书；不跟随重定向，响应最多读取513字节、单连接超时5秒。DNS 解析使用系统 resolver，其超时由操作系统控制，公网部署前需额外设置解析预算与并发限额。

挑战单次使用，重新生成后旧挑战不可用；验证过期后采集密钥暂停生效，再次验证成功可恢复未撤销密钥。密钥只允许 `POST /logs`，不能读数据、写回答或管理站点；数据库保存 SHA-256 摘要，不存明文。撤销后不可恢复。轮换顺序：签发新密钥 → 更新采集器环境变量 → 撤销旧密钥。

管理员 token 仍可手动导入未验证站点，供本地演示与人工证据录入；自动采集应使用站点专属密钥，不向采集机分发管理员 token。验证证明域名控制，不证明后续日志真实性，也不是合规授权替代品。

## P02：Nginx JSONL 文件采集

每行一个 JSON 对象，status 必须是数字：

```json
{"time_iso8601":"2026-10-29T10:00:00+08:00","url":"https://example.com/docs","status":200,"http_user_agent":"GPTBot"}
```

Nginx 示例（放在 http 上下文，采集范围限本站；使用 `escape=json` 防止字段中的引号破坏记录）：

```nginx
log_format xing_json escape=json '{"time_iso8601":"$time_iso8601","url":"https://example.com$uri","status":$status,"http_user_agent":"$http_user_agent"}';
access_log /var/log/nginx/xing.jsonl xing_json;
```

将 example.com 替换为已登记域名。示例刻意不用 `$request_uri`（可能含敏感查询参数），不用未经约束的 Host 请求头决定域名。Nginx 配置示例未在真实 Nginx 实例执行，启用前由管理员验证。

在项目根目录运行，`GEO_INGEST_TOKEN` 从本机安全环境中配置，不提交到 Git：

```powershell
python -m geo.collector --endpoint http://127.0.0.1:8000 --site SITE_ID --source nginx-origin-1 --state data/collector.json data/access.jsonl data/access.jsonl.1
```

只传实际存在的日志路径，可传多个保留的重命名轮转文件。本地 HTTP 仅允许数字回环地址；非本机接口必须 HTTPS。客户端拒绝重定向以防凭证转发。每轮每个文件最多100条，退出后重复执行继续推进；本版不安装后台定时器。输出 acknowledged（含去重确认）、invalid_lines、incomplete_lines；退出码0成功，2遇到坏行，1传输/配置/状态失败。

### 持久化与恢复契约

1. 状态文件绑定 endpoint/site/source，不能换目标继续使用。保存文件设备号/inode、随机代次、已确认字节位置和前缀校验值。
2. 事件编号为 `代次:字节偏移`。待发送批次先落地 outbox，再调用服务；仅服务确认后推进位点。
3. 服务已接收但确认丢失时，下次原样重放 outbox；服务幂等键使报告不增数。
4. 同一 state 文件加操作系统文件锁，防止多进程并发推进。状态以临时文件+fsync+原子替换写入；仍需备份，未承诺磁盘损坏或突然断电下的完整恢复。
5. 完整坏行阻塞该文件后续处理并报告错误，不跳过。末尾没有换行则视为尚未写完，下次再读。
6. 支持保留文件的 rename 轮转；新文件获得新代次，旧文件沿原 inode 继续。须保留旧文件直到被采集完，并把它们纳入每轮路径列表。
7. 不支持 copytruncate、原地改写、压缩轮转、inode 不稳定的网络文件系统。检测到长度缩短或前缀变化时停止。仅检查已确认前缀最多4096字节，不能检测所有历史原地修改。轮转文件已丢失时无法补回未读数据。

不要删除 checkpoint 作为普通重试手段，否则会生成新代次导致重复数据；状态损坏、路径来源变更需人工核对。state/outbox 内可能含规范化 URL 和 UA，保存在受限 data/ 目录，保留时间由运营者管理。

## 本地验收

```powershell
python -m unittest discover -s tests -v
```

覆盖旧数据库升级、验证过期与错误证明、内网解析拦截、HTTPS固定目标与不跳转、HTTP签发/撤销、站点密钥权限、并发重复导入、确认丢失、网络失败、轮转、半行、坏行、分批积压及目标错配。所有站点与日志为合成测试，不代表真实联网验证已验收。
