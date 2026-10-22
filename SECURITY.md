# 安全与隐私说明

这个项目要接触你的 cookie 和 189 账号密码，所以把边界写清楚。

## 数据去哪了

| 位置 | 存了什么 | 谁能看到 |
| --- | --- | --- |
| GitHub Secrets | 各站点 cookie / 189 账号密码 | 有仓库 admin 权限的人（写权限的人只能使用、看不到明文） |
| GitHub Actions 日志 | 签到结果文字，cookie / 密码已替换成 `abcd***wxyz` | 能看到该仓库 Actions 的人（私有仓库＝协作者） |
| Actions 的 Summary | 站点名、账号昵称、余额、鸡腿数 | 同上 |
| 通知渠道 | 完整汇总文本 | 对应群 / 频道里的人 |
| `data/status.json` | 最近一次结果 + 最近 30 次历史 | 本机 / 容器 + 面板登录用户 |
| `data/panel_auth.json` | PBKDF2 派生值、随机盐、会话签名密钥（**没有明文口令**） | 同上，文件权限 0600 |
| `data/settings.json` | **在网页「设置」里保存过的 cookie / 账号密码 / 代理（明文）** | 同上，文件权限 0600 |
| `.env`（NAS 部署时） | 同上，明文 | 能读这个文件的人（建议 `chmod 600`） |

不存的东西：cookie / 密码不写进 `status.json`、不进通知、不进 git、不进镜像。

> 如果你只在 `.env` 或 Secrets 里配凭据、从没在网页上改过，那 `data/settings.json` 不会存在，凭据不落盘。
> 一旦在网页上保存过，它就是一份明文凭据 —— 和 `.env` 同等敏感，别放共享目录、别打包给别人。

## 面板的安全默认

- 未登录访问 `/`、`/settings` 和 `/api/*` 一律 302 或 401，没有任何"游客可读"模式
- 口令用 PBKDF2-HMAC-SHA256（12 万次迭代）+ 16 字节随机盐
- 会话是 HMAC-SHA256 签名的过期时间戳，`HttpOnly; SameSite=Lax`，默认 7 天
- 改口令会轮换签名密钥，所有旧会话立即失效；改完给当前浏览器发一张新票据，不用重新登录
- 登录失败按 IP 限速（5 分钟内 6 次），超限返回 429
- 页面不加载任何外部资源，CSP 为 `default-src 'self'`，附 `X-Frame-Options: DENY`
- 会话 cookie 是 `HttpOnly; SameSite=Lax`；套了 HTTPS 反代就把 `PANEL_COOKIE_SECURE=true` 加上
- 所有写接口（`/api/run`、`/api/settings`、`/api/password`、`/api/check`）只接受 POST，
  且校验 `Origin` 必须同源；配合 `SameSite=Lax`，跨站伪造请求会被 403
- **浏览器永远拿不到凭据明文**：`/api/settings` 返回的密文字段是 `A2=a***5678` 这种掩码，
  保存时留空代表"沿用原值"，明文只在服务端内存里拼接
- 请求体上限 256 KB，超出直接丢弃，不做无界读取
- 不记录访问日志（避免把 URL / 参数写进容器日志）
- 不想开放网页改配置：`PANEL_ALLOW_EDIT=false`，设置页变只读，只保留状态查看
- 默认只该在局域网用；要暴露到公网，必须前面套 HTTPS 反代（Caddy / Nginx / 群晖反代），不要裸奔

## 开源前的检查清单（已执行）

- [x] `.gitignore` 覆盖 `.env*`、`config.json`、`data/`、`logs/`、`node_modules/`
- [x] 仓库内没有任何真实 cookie / 密码，示例文件全部留空
- [x] 运行日志脱敏（cookie / 密码 / A2 都替换成掩码）
- [x] 不上传 Actions artifact（没有日志落盘，减少暴露面）
- [x] `permissions: contents: read`，工作流不需要写权限
- [x] `npm ci` 按 `package-lock.json` 安装，避免依赖漂移
- [x] 面板默认需要口令，口令不入库
- [x] LICENSE 保留上游 MIT 声明与致谢

## 凭据泄漏了怎么办

1. 立刻在目标站点「退出所有设备 / 修改密码」，旧 cookie 会随之失效
2. 更新对应 Secret 或 `.env`
3. 如果曾经提交过 `config.json` / `.env`：仅删除文件不够，git 历史里还在 —— 用 `git filter-repo` 清历史，或直接删仓库重建

## 报告问题

发现安全问题请开一个 **私有** security advisory（仓库 Security 标签页 → Report a vulnerability），不要在公开 issue 里贴 cookie、日志原文或网关地址。
