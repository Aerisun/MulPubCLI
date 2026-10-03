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

| 平台参数 | 平台名称 |
|---------|---------|
| `xiaohongshu` | 小红书 |
| `zhihu` | 知乎 |
| `toutiao` | 今日头条 |

---

## login — 登录

```
mulpubcli login <platform> [--method qr|sms] [--refresh] [--poll|--confirm|--cookie-file FILE]
```

`login` 是幂等的：反复执行会**优先复用有效状态**，而不是无谓生成新二维码。

- 小红书 / 今日头条：默认二维码登录。已有有效登录态直接返回 `authenticated`；登录态失效会自动生成新二维码替换；未过期二维码直接复用。
- 知乎：通常用浏览器导出的 Cookie 导入登录态（见 `--cookie-file`），二维码作为回退方式。

```bash
# 生成二维码（输出 JSON 含 qr_image 路径）
mulpubcli login toutiao

# 用手机 App 扫码后，做一次"确认"轮询，完成登录
mulpubcli login toutiao --poll

# 强制重新登录（生成全新二维码）
mulpubcli login toutiao --refresh

# 小红书短信登录
mulpubcli login xiaohongshu --method sms            # 输入手机号，发送验证码
mulpubcli login xiaohongshu --method sms --confirm  # 输入验证码完成登录

# 知乎：从浏览器导出的 Cookie 文件导入登录态
mulpubcli login zhihu --cookie-file cookies.txt
```

**选项说明：**

| 选项 | 说明 |
|------|------|
| `--method qr` | 二维码登录（默认） |
| `--method sms` | 短信登录（仅小红书） |
| `--poll` | 扫码后单次确认扫码结果并完成登录（只查一次，不会反复轮询） |
| `--confirm` | 输入短信验证码确认（仅 `--method sms`） |
| `--cookie-file FILE` | 从文件导入浏览器导出的知乎 Cookie（原始 Cookie 头 / Chrome/Playwright JSON / cookies.txt） |
| `--refresh` | 强制刷新二维码 / 强制重新导入知乎 Cookie（忽略现有登录态） |

**输出状态说明：**

| status | 含义 |
|--------|------|
| `authenticated` | 已有有效登录态，可直接发布 |
| `waiting` | 二维码已生成/复用，等待扫码 |
| `scanned` | 已扫码，等待 App 确认 |
| `expired` | 二维码/登录态已过期 |
| `error` | 发生错误，message 字段有详情 |

凭证保存在 `.storage/auth/<platform>.json`（权限 600）。

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

删除指定平台的登录凭证和二维码，下一次 `login` 完全从头开始。用于清理卡死或需要换账号登录的场景。

```bash
mulpubcli reset zhihu
```

---

## publish — 发布文章

```
mulpubcli publish <platform> --article FILE [--force]
```

正式发布（公开可见）。包含以下步骤：
1. 检查本地幂等账本，24 小时内相同内容不重复提交
2. 验证账号登录态
3. 上传封面图
4. 上传正文中引用的所有本地图片
5. 提交文章
6. 回读核验

封面在稿件内用 `<!-- cover: 路径 -->` 指令指定（详见 [docs/publishing.md](publishing.md)），不再需要单独的 `--cover` 参数。

```bash
mulpubcli publish zhihu --article article.md
mulpubcli publish toutiao --article article.md
mulpubcli publish xiaohongshu --article article.md
```

| 选项 | 说明 |
|------|------|
| `--article FILE` | Markdown 稿件路径（第一行为 `# 标题`，封面用 `<!-- cover: 路径 -->` 指令） |
| `--force` | 强制发送：跳过本地 24 小时去重与 pending 记录拦截，直接重新投稿并记账 |

**输出状态说明：**

| status | 含义 |
|--------|------|
| `published` | 发布成功，`url` 字段含公开链接 |
| `pending` | 已提交但尚未核验（可能在审核中），不会重发 |
| `failed` | 发布失败，原因见 `message`，未提交到平台 |
| `skipped` | 本地已有记录，跳过不重发 |

→ 详见 [docs/publishing.md](publishing.md)

---

## draft — 保存草稿

```
mulpubcli draft <platform> --article FILE [--force]
```

仅支持 `zhihu` 和 `toutiao`。流程与 publish 相同，但结果为草稿（不公开发布）。

```bash
mulpubcli draft zhihu --article article.md
```

---

## list — 实时发布列表

```
mulpubcli list [<platform>] [--json]
```

查看已发布文章的实时列表。小红书 / 头条直接读平台最新数据（已删除的文章自然消失）；知乎无"已发布列表"接口，走本地发布台账兜底（并实时回查单篇状态）。

```bash
mulpubcli list                     # 全部平台（可读表格）
mulpubcli list xiaohongshu         # 只看小红书
mulpubcli list --json              # 输出原始 JSON，供机器使用
```

表格列：发布时间 / 平台 / 标题 / 编号（ID）。

---

## verify — 回查文章状态

```
mulpubcli verify [--id ID] [--platform <platform>] [--article FILE] [--json]
```

回查文章在线状态，两种用法：

- **`--id ID`**：查单篇。文章 ID 全局唯一，无需指定平台，自动识别所属平台。可选 `--article` 配合内容指纹核验。
- **`--platform <platform>`**（或省略全部参数）：刷新整平台列表状态，只汇报**变化**（新发布几篇 / 删除几篇）。检测到已删除的文章会询问是否将其从本地列表中移除。

```bash
mulpubcli verify --id 7385929102934                  # 回查单篇，自动识别平台
mulpubcli verify --id 1234567890 --article article.md  # 配合内容指纹核验
mulpubcli verify --platform zhihu                    # 刷新知乎，汇报变化并移除已删除项
mulpubcli verify                                    # 刷新全部平台
mulpubcli verify --json                              # 输出原始 JSON，跳过删除交互（供机器用）
```

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
├── auth/        # 登录凭证（权限 600，永久）
│   ├── xiaohongshu.json
│   ├── zhihu.json
│   └── toutiao.json
├── qr/          # 临时二维码 PNG（10 分钟自动清理）
│   └── toutiao-login.png
├── results/     # 发布账本（权限 600，永久）
│   └── toutiao-abc123.json
└── tmp/         # 临时中间文件
```

`.storage/` 已加入 `.gitignore`，不会被提交到版本库。
