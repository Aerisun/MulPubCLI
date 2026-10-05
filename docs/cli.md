# CLI 使用手册

完整的 `mulpubcli` 命令行参考。

---

## 安装

```bash
pip install -e .
```

安装后可直接使用 `mulpubcli` 命令，也可用 `python -m mulpubcli`。

---

## 全局选项

```
mulpubcli [--root DIR] [--proxy URL] <command> ...
```

| 选项 | 说明 |
|------|------|
| `--root DIR` | 覆盖项目根目录（默认自动定位 `pyproject.toml` 所在目录），一般用于测试 |
| `--proxy URL` | HTTP(S)/SOCKS5 代理出口，例如 `--proxy http://127.0.0.1:7890`（用于干净出口登录） |

---

## 支持平台

| 平台参数 | 平台名称 | `publish` | `draft` | `list` / `verify` |
|---------|---------|-----------|---------|-------------------|
| `xiaohongshu` | 小红书 | 支持 | 不支持 | 支持 |
| `zhihu` | 知乎 | 支持 | 支持 | 支持 |
| `toutiao` | 今日头条 | 支持 | 支持 | 支持 |
| `netease` | 网易号 | 支持 | 支持 | 支持 |
| `sohu` | 搜狐号 | 支持图文直接投稿 | 不作为对外命令 | 支持 |

不支持的操作会返回 `status: failed` 和 `verification: unsupported`，不会提交到平台。

---

## login — 登录

```
mulpubcli login <platform> [--method qr|sms] [--poll|--confirm|--cookie-file FILE]
mulpubcli login <netease|sohu> --refresh [--phone PHONE] [--password PASS]
```

知乎、小红书、今日头条的默认二维码登录会在同一条命令中先输出 `waiting` 和二维码路径，保持进程等待；扫码并通过账号核验后输出 `authenticated`。小红书和头条在二维码失效时返回 `expired`，下次执行 `login` 获取新码；知乎跟随登录页自动换码，并更新同一路径下的图片。已保存且实时核验有效的登录态直接返回 `authenticated`。这三个平台如需清除旧登录态，先执行 `reset <platform>`，再执行 `login <platform>`；`--refresh` 对它们会直接报错。

知乎使用真实浏览器取得扫码地址，再生成独立的高清二维码 PNG；扫码状态由登录页自行查询，CLI 只监听页面响应，不另发查询请求。小红书和头条使用各自的二维码接口。三者最终都把可发布凭证保存在 `.storage/auth/<platform>.json`。

网易号和搜狐号使用按需启动的浏览器登录。已有凭证经实时核验有效时，`login` 会直接返回账号 ID、名称和凭证路径，不再提示输入账号密码；`--refresh` 表示主动刷新当前账号的续期凭证。默认直接使用已保存的手机号和密码，不再询问输入；没有保存的信息时可用环境变量或显式参数提供。核验新凭证后才替换旧凭证，失败时保留旧凭证。若显式提供了与已保存信息不同的手机号或密码，会用干净的浏览器会话实际登录，并确认仍是原账号后才更新保存的信息；切换账号请先执行 `reset`。搜狐会为每个手机号复用同一浏览器配置，并从已核验的原账号凭证补回会话 Cookie，优先直接进入后台；只有授权失效时才用账号密码登录。自动 `--refresh` 和后台自动续期遇到搜狐短信授权都会停止并保留旧凭证，不发送短信或等待输入；需要人工处理时可走交互式登录流程。一次账号密码登录成功后，工具将手机号和密码保存在 `.storage/auth/<platform>-login.json`（权限 600），与 Cookie 凭证文件分开。后续执行需要登录态的命令时，若账号接口明确确认凭证失效，会用保存的信息自动重新登录、核验仍是原账号，然后继续原操作。也可用 `NETEASE_PHONE` / `NETEASE_PASS` 或 `SOHU_PHONE` / `SOHU_PASSWORD` 提供续期信息。已有安装若只有 Cookie 而没有保存的账号密码，需要用环境变量或显式参数执行一次 `login netease --refresh` 或 `login sohu --refresh`。网络故障不会触发重新登录；提交结果不明时也不会自动重发。

网易号登录会在浏览器跳转完成后收集 `163.com` 及其子域名的 Cookie，并在 HTTP 核验后保留它们。搜狐打开文章编辑页后若进入 `/clientAuth`，会在浏览器里点击“获取验证码”，确认发送接口成功后才提示输入短信。若搜狐先要求页面滑块，命令会临时提供一个仅监听 `127.0.0.1` 的小窗口地址；窗口显示原浏览器的验证码区域，并把拖动动作传回原会话。滑块窗口最多等待 2 分钟；短信验证码若接口未提供有效期，也最多等待 2 分钟（可用 `SOHU_SMS_CODE_TTL_SECONDS` 调整）。到期后登录结束。窗口只在验证期间运行，不复制 Cookie，不启动第二个浏览器。新凭证通过编辑页和账号接口核验后才替换原凭证。

远程服务器上可在自己电脑另开 SSH 隧道，端口取命令输出的小窗口地址中的端口，然后在本机浏览器打开该地址：

```bash
ssh -N -L <端口>:127.0.0.1:<端口> <你的SSH地址>
```

大服务可创建 `SohuLogin(..., on_challenge=callback)`；回调收到临时本地 URL 后，通过大服务同源反向代理将其放进弹窗或 iframe。代理须同时转发该路径下的普通 HTTP 请求和 `ws` WebSocket 升级请求，保持路径后缀不变。窗口只含验证码图像，没有额外文字或按钮，背景透明且无边距；主服务可把 iframe 放在任意位置。验证码首次显示或尺寸变化时，窗口向同源父页面发送 `{type: 'mulpubcli:sohu:challenge-size', width, height}`，主服务可据此把 iframe 设为对应像素尺寸。短信发送成功时发送 `{type: 'mulpubcli:sohu:sms-sent'}`，主服务可关闭弹窗并显示短信输入框。URL 含一次性访问令牌，应只交给当前登录用户；验证码结束后本地服务即关闭。没有人打开窗口时不会持续截图。

```bash
# 生成二维码并持续等待扫码确认
mulpubcli login toutiao

# 小红书和知乎也是一条命令完成登录
mulpubcli login xiaohongshu
mulpubcli login zhihu

# 清理旧登录态后重新登录（小红书、知乎、头条）
mulpubcli reset toutiao
mulpubcli login toutiao

# 网易号、搜狐号：已有有效登录态时直接复用
mulpubcli login netease
mulpubcli login sohu

# 主动刷新当前账号的续期凭证
mulpubcli login netease --refresh
mulpubcli login sohu --refresh
mulpubcli login sohu --refresh --show-browser  # 页面验证码需人工操作且有图形显示时

# 小红书短信登录
mulpubcli login xiaohongshu --method sms            # 输入手机号，发送验证码
mulpubcli login xiaohongshu --method sms --confirm  # 输入验证码完成登录

```

**选项说明：**

| 选项 | 说明 |
|------|------|
| `--method qr` | 二维码登录（默认） |
| `--method sms` | 短信登录（仅小红书） |
| `--poll` | 兼容旧流程：对小红书或头条的已有二维码只检查一次 |
| `--confirm` | 输入短信验证码确认（仅 `--method sms`） |
| `--refresh` | 仅网易、搜狐：自动读取已保存信息并刷新当前账号的续期凭证，无需再次输入；新凭证核验失败时保留旧凭证。其他平台用 `reset` 后重新 `login` |
| `--show-browser` | 搜狐登录显示浏览器窗口以完成人工页面验证，需要图形显示 |
| `--cookie-file FILE` | 保留参数；当前实现尚未读取该文件，不能用于导入网易号登录态 |

**输出状态说明：**

| status | 含义 |
|--------|------|
| `authenticated` | 登录态有效；图文发布资格仍由平台检查 |
| `waiting` | 二维码已生成/复用，当前命令正在等待扫码 |
| `scanned` | 已扫码，等待 App 确认 |
| `expired` | 二维码/登录态已过期 |
| `error` | 发生错误，message 字段有详情 |

登录完成结果包含 `account_id`、`username`、`expires_at` 和 `cookie_expirations`。`cookie_expirations` 记录各 Cookie 实际提供的到期时间；`expires_at` 是已知登录 Cookie 中最早的到期时间。会话 Cookie 或平台未提供到期属性时为 `null`，不会推测一年等固定期限。这些时间不保证平台不会提前撤销登录态，`session` 命令仍会联网核验。Cookie 凭证保存在 `.storage/auth/<platform>.json`（权限 600）；输出不会包含 Cookie 值或密码。

知乎登录页会自行刷新失效的二维码，CLI 同步更新独立的二维码图片。扫码等待从首次提供二维码起最多持续 2 分钟；自动换码不会延长这个时间。到期后输出 `expired` 并删除旧图片，重新执行 `login zhihu` 可获取新码。

---

## session — 实时查看登录状态

```
mulpubcli session [<platform>]
```

**实时联网核验**各平台登录态有效性，而不是只读本地文件。

```bash
mulpubcli session          # 查看全部平台
mulpubcli session zhihu    # 只看知乎
```

每个平台做一次轻量已认证探测（小红书读发布状态接口，知乎 / 头条查账号信息），返回 `status`：

| status | 含义 |
|--------|------|
| `authenticated` | 凭证有效，可直接发布 |
| `needs_login` | 凭证缺失或已失效，需重新 `login` |
| `unreachable` | 网络或服务端暂态，登录态未知（不误报"未登录"） |

---

## reset — 清理登录状态

```
mulpubcli reset <platform>
```

删除指定平台的登录凭证和二维码；网易号和搜狐号还会删除保存的手机号、密码及各自的浏览器配置，知乎会清理自己的浏览器配置，小红书会清理待扫码及短信登录会话。下一次 `login` 从头开始，适合清理卡死状态或切换账号。

```bash
mulpubcli reset zhihu
```

---

## publish — 发布文章

```
mulpubcli publish <platform> --article FILE [--declaration VALUE]
```

直接提交公开投稿请求。平台可能需要审核，提交成功不等于已经公开可见。包含以下步骤：
1. 验证账号登录态
2. 上传封面图
3. 上传正文中引用的所有本地图片
4. 提交文章
5. 用 `verify` 回读平台状态

本地不再按已有发布记录或过去 24 小时的提交次数拦截请求；每次调用都会发起新的平台提交，并分别保留本地记录。

封面在稿件内用 `<!-- cover: 路径 -->` 指令指定（详见 [docs/publishing.md](publishing.md)），不再需要单独的 `--cover` 参数。

```bash
mulpubcli publish zhihu --article article.md
mulpubcli publish toutiao --article article.md
mulpubcli publish xiaohongshu --article article.md
mulpubcli publish netease --article article.md
mulpubcli publish sohu --article article.md
mulpubcli publish sohu --article article.md --declaration fiction
```

| 选项 | 说明 |
|------|------|
| `--article FILE` | Markdown 稿件路径（第一行为 `# 标题`，封面用 `<!-- cover: 路径 -->` 指令） |
| `--declaration` | 仅搜狐：`none`（无需声明，默认）、`fiction`（虚构演绎）、`ai`（AI 生成）、`marketing`（营销）、`reprint`（转载）、`opinion`（个人观点） |

搜狐 `publish` 直接请求当前编辑器使用的图文公开投稿接口，不经草稿接口。搜狐账号无图文发布资格时，平台会拒绝投稿；CLI 会保留拒绝原因。本轮只验证了接口结构和只读列表，没有执行真实公开投稿。

发布和存稿输出统一包含 `platform`、`id`、`title`、`status`、`message`、`url`、`verification`。`id` 是平台文章 ID；在平台未返回 ID 时为 `null`。`url` 只有在取得可核验链接时才提供。

**输出状态说明：**

| status | 含义 |
|--------|------|
| `published` | 平台回读确认已发布，`url` 字段含公开链接 |
| `pending` | 已提交但尚未核验（可能在审核中）；工具不会自动重试 |
| `failed` | 投稿前失败或被平台明确拒绝，原因见 `message` |

→ 详见 [docs/publishing.md](publishing.md)

---

## draft — 保存草稿

```
mulpubcli draft <platform> --article FILE
```

支持 `zhihu`、`toutiao` 和 `netease`。流程与 publish 相同，但结果为草稿（不公开发布）。搜狐使用 `publish sohu` 直接投稿。

```bash
mulpubcli draft zhihu --article article.md
mulpubcli draft toutiao --article article.md
mulpubcli draft netease --article article.md
```

---

## list — 本工具发布文章的跟踪列表

```
mulpubcli list [<platform>] [--json]
```

查看本工具提交并跟踪的文章。已有平台文章 ID 时，小红书、头条、网易号、搜狐号读取平台作品列表核对状态；知乎按本地记录中的文章 ID 逐篇查询。未由本工具提交或已取消跟踪的文章不会自动导入列表。平台列表暂时读不到旧文章时，仍保留本地记录并标明尚未确认，避免把分页遗漏或接口故障误判为删除。平台拒绝投稿且未取得文章 ID、链接的失败尝试只保留在 `status` 历史中，不占发布列表；同一原稿的旧记录缺标题时，若其他渠道记录有唯一一致的原稿指纹，会用它补齐标题。

```bash
mulpubcli list                     # 全部平台（可读表格）
mulpubcli list xiaohongshu         # 只看小红书
mulpubcli list sohu                 # 搜狐图文及审核状态
mulpubcli list --json              # 输出原始 JSON，供机器使用
```

表格列：发布时间 / 平台 / 标题 / 编号 / 状态 / 链接 / 核验说明。平台显示中文名，时间按北京时间显示，兼容秒、毫秒、微秒时间戳。编号优先显示平台文章 ID，尚未取得时显示本地 `tracking_id`。`--json` 每篇文章有 `id`、`tracking_id`、`url`、`status`；平台键仍是英文命令参数。无法确认的项目会有 `check`。小红书需要平台提供的分享令牌才能生成可靠的直达链接；缺少令牌时 `url` 为 `null` 并解释原因。网易和知乎草稿返回编辑页链接；头条只有拿到公开 `item_id` 才返回公开文章链接。

## list-delete — 取消跟踪

```
mulpubcli list-delete ID [ID ...]
```

按空格传入一个或多个 `list` 显示的编号。命令先列出将取消跟踪的文章、发布时间和状态，再要求输入 `y` 确认。直接回车或输入 `n` 时不会修改记录；有任何编号找不到或对应多条记录时，整批操作都会停止。编号冲突时可使用本地 `tracking_id`。

```bash
mulpubcli list-delete 2089828867179536901 toutiao-59399ec9c0373dce1a6b
```

取消跟踪只让记录退出 `list` 和批量 `verify`，不会删除平台文章，`status` 仍可查看历史记录。之后再次提交相同稿件会创建并跟踪新的提交记录。

---

## verify — 回查文章状态

```
mulpubcli verify [--id ID] [--platform <platform>] [--article FILE] [--json]
```

回查文章在线状态，两种用法：

- **`--id ID`**：查单篇。已保存的 ID 从本地记录识别平台；没有记录时按 ID 形态尝试候选平台，也可用 `--platform` 明确指定。可选 `--article` 对照原稿核验。
- **`--platform <platform>`**（或省略全部参数）：刷新本工具正在跟踪的文章，对已保存且有文章 ID 的项目逐篇回读，显示每篇状态、链接和核验说明。只凭列表缺席或临时接口错误不会认定文章已删除，也不会删除本地记录。

回查确认文章已删除或下线时，状态为 `deleted`，公开链接会清空，但跟踪记录仍保留，只有 `list-delete` 才会取消跟踪。小红书要求完整作品列表无该笔记，且单篇详情明确返回 `-9106`（该笔记已被删除）；搜狐号使用单篇状态码 7；网易号使用单篇 `postState=7`；今日头条要求详情状态码 4 且删除原因为 `pgc_delete`；知乎要求公开文章与当前账号草稿接口均返回 404（也可能是平台下架）。接口返回 401 时显示 `unreachable`，不会根据列表缺席猜测已删除。

```bash
mulpubcli verify --id 7385929102934                  # 回查单篇，自动识别平台
mulpubcli verify --id 1234567890 --article article.md  # 配合内容指纹核验
mulpubcli verify --platform zhihu                    # 刷新知乎并逐篇回查
mulpubcli verify                                    # 刷新全部平台
mulpubcli verify --json                              # 返回各平台汇总及 articles 逐篇结果
```

`verification` 表示核验深度：`verified` 是平台状态与已实现的原稿证据核对通过；`published` 是平台确认已发布，但缺少足够证据做完整内容比对；`mismatch` 是回读内容或状态不一致；`unavailable` 是暂时无法完成回查。小红书成稿后可能重写图片 ID：能读取原图时按像素与顺序核对；原图 CDN 暂不可读时，若每张图的尺寸和上传大小按顺序完全一致，会返回 `published` 并明确提示未做像素核对，`list` 也保留这条说明。知乎和头条公开后的详情可能把封面移到正文首图，回查会同时核对这两种实际结构。搜狐逐篇核对标题、正文和已记录的配图顺序，但尚未经过真实发布验证。旧记录若缺少远端文章 ID，无法推导直达链接，会在列表里提示人工核对；不会为了补链接重新投稿。

---

## status — 查看发布记录

```
mulpubcli status [--platform <platform>]
```

查看本地幂等账本中的发布历史。

```bash
mulpubcli status                    # 全部平台
mulpubcli status --platform zhihu  # 仅知乎
```

记录存储在 `.storage/results/`。

---

## storage — 查看存储状态

```
mulpubcli storage
```

诊断命令，显示：
- 各子目录文件数量
- 各平台凭证有效期和最后更新时间
- 当前临时二维码列表及生成时长

```bash
mulpubcli storage
```

---

## 内部存储结构

```
.storage/
├── auth/        # 登录凭据与浏览器配置（目录权限 700）
│   ├── xiaohongshu.json
│   ├── zhihu.json
│   ├── toutiao.json
│   ├── netease.json
│   ├── sohu.json
│   ├── zhihu-profile/   # CLI 管理的浏览器配置
│   ├── sohu-profile/    # 网易沿用的浏览器配置目录名
│   ├── sohu-<手机号哈希>/  # 每个搜狐手机号独立的浏览器配置
│   ├── auto/            # 手工保存的浏览器用户数据
│   └── ck.txt           # 手工导出的 Cookie 文件
├── qr/          # 临时二维码 PNG（10 分钟自动清理）
│   └── toutiao-login.png
├── results/     # 发布账本（权限 600，永久）
│   └── toutiao-abc123.json
└── tmp/         # 临时中间文件
```

`.storage/` 已加入 `.gitignore`，不会被提交到版本库。登录流程使用自己的平台浏览器配置目录；手工保存的 Chromium 用户数据和 Netscape Cookie 文件可分别放在 `.storage/auth/auto/` 与 `.storage/auth/ck.txt`，CLI 不会自动读取它们。若有外部脚本使用这些文件，需要把路径改为新位置。Cookie 文件应设置为仅当前用户可读写（`chmod 600 .storage/auth/ck.txt`）。项目根目录的 `auto/` 和 `ck.txt` 也有单独的忽略规则，以免误提交。
