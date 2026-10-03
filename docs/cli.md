# CLI 使用手册

完整的 `mutipubcli` 命令行参考。

---

## 安装

```bash
pip install -e .
```

安装后可直接使用 `mutipubcli` 命令，也可用 `python -m mutipubcli`。

---

## 全局选项

```
mutipubcli [--root DIR] <command> ...
```

| 选项 | 说明 |
|------|------|
| `--root DIR` | 覆盖项目根目录（默认自动定位 `pyproject.toml` 所在目录），一般用于测试 |

---

## 支持平台

| 平台参数 | 平台名称 |
|---------|---------|
| `xiaohongshu` | 小红书 |
| `zhihu` | 知乎 |
| `toutiao` | 今日头条 |

---

## login — 扫码登录

```
mutipubcli login <platform> [--method qr|sms] [--refresh]
```

`login` 是幂等的：反复执行会**优先复用有效状态**，而不是无谓生成新二维码。

- 已有**有效登录态**：内部做一次只读校验，确认有效后直接返回 `authenticated`，不再生成二维码。
- 登录态**已过期/失效**：自动用全新匿名设备会话生成二维码替换旧会话，并返回新二维码。
- 已有**未过期的二维码**：直接复用，不重新生成。
- 二维码**过期**：自动刷新并返回新二维码。

```bash
# 生成二维码（输出 JSON 含 qr_image 路径）
mutipubcli login toutiao

# 用手机 App 扫码后，做一次"确认"轮询，完成登录
mutipubcli login toutiao --poll

# 强制重新登录（生成全新二维码）
mutipubcli login toutiao --refresh

# 小红书短信登录
mutipubcli login xiaohongshu --method sms            # 输入手机号，发送验证码
mutipubcli login xiaohongshu --method sms --confirm  # 输入验证码完成登录
```

**选项说明：**

| 选项 | 说明 |
|------|------|
| `--method qr` | 二维码登录（默认） |
| `--method sms` | 短信登录（仅小红书） |
| `--poll` | 扫码后单次确认扫码结果并完成登录（只查一次，不会反复轮询） |
| `--refresh` | 强制刷新二维码，彻底重新登录 |

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

## session — 查看登录状态

```
mutipubcli session [<platform>]
```

只读查看各平台**本地登录状态**，不发起网络请求，可随时执行，不轮询。

```bash
mutipubcli session          # 查看全部平台
mutipubcli session zhihu    # 只看知乎
```

每个平台输出 `status`（`needs_login` / `pending_scan` / `expired` / `blocked` / `authenticated`）、凭证路径、二维码年龄等。

> `login` 会做一次线上校验确认有效；`session` 只看本地状态，两者配合即可，无需反复轮询。

---

## reset — 清理登录状态

```
mutipubcli reset <platform>
```

删除指定平台的登录凭证和二维码，下一次 `login` 完全从头开始。用于清理卡死或需要换账号登录的场景。

```bash
mutipubcli reset zhihu
```

---

## publish — 发布文章

```
mutipubcli publish <platform> --article FILE --cover FILE
```

正式发布（公开可见）。包含以下步骤：
1. 检查本地幂等账本，24 小时内相同内容不重复提交
2. 验证账号登录态
3. 上传封面图
4. 上传正文中引用的所有本地图片
5. 提交文章
6. 回读核验

```bash
mutipubcli publish zhihu \
  --article article.md \
  --cover   cover.jpg

mutipubcli publish toutiao \
  --article article.md \
  --cover   cover.jpg

mutipubcli publish xiaohongshu \
  --article article.md \
  --cover   cover.jpg
```

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
mutipubcli draft <platform> --article FILE --cover FILE
```

仅支持 `zhihu` 和 `toutiao`。流程与 publish 相同，但结果为草稿（不公开发布）。

```bash
mutipubcli draft zhihu --article article.md --cover cover.jpg
```

---

## verify — 查询文章状态

```
mutipubcli verify <platform> --id ARTICLE_ID [--article FILE]
```

通过平台 ID 回查已提交的文章状态。

```bash
mutipubcli verify toutiao --id 7385929102934
mutipubcli verify zhihu --id 1234567890 --article article.md
```

| 参数 | 说明 |
|------|------|
| `--id` | 平台分配的文章 ID（必须） |
| `--article` | 原稿路径（可选），用于内容指纹核验 |

---

## status — 查看发布记录

```
mutipubcli status [--platform <platform>]
```

查看本地幂等账本中的发布历史。

```bash
mutipubcli status                    # 全部平台
mutipubcli status --platform zhihu  # 仅知乎
```

记录存储在 `.storage/results/`。

---

## storage — 查看存储状态

```
mutipubcli storage
```

诊断命令，显示：
- 各子目录文件数量
- 各平台凭证有效期和最后更新时间
- 当前临时二维码列表及生成时长

```bash
mutipubcli storage
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
