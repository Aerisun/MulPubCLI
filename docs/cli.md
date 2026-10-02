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
mutipubcli login <platform> [--method qr|sms] [--poll | --refresh | --confirm]
```

**典型流程：**

```bash
# 1. 生成二维码（输出 JSON 含 qr_image 路径）
mutipubcli login toutiao

# 2. 用手机 App 扫描二维码后，轮询一次结果
mutipubcli login toutiao --poll

# 3. 二维码超时后刷新
mutipubcli login toutiao --refresh

# 小红书短信登录
mutipubcli login xiaohongshu --method sms          # 输入手机号，发送验证码
mutipubcli login xiaohongshu --method sms --confirm  # 输入验证码完成登录
```

**选项说明：**

| 选项 | 说明 |
|------|------|
| `--method qr` | 二维码登录（默认） |
| `--method sms` | 短信登录（仅小红书） |
| `--poll` | 轮询一次扫码结果，不重新生成二维码 |
| `--refresh` | 强制刷新二维码（旧码自动失效） |
| `--confirm` | 读取 stdin 输入短信验证码并提交 |

**输出状态说明：**

| status | 含义 |
|--------|------|
| `waiting` | 二维码已生成，等待扫码 |
| `scanned` | 已扫码，等待 App 确认 |
| `authenticated` | 登录成功，凭证已保存 |
| `missing_phone` | 扫码成功但账号未绑定手机号，需在 App 完成绑定 |
| `expired` | 二维码已过期，执行 `--refresh` 重新生成 |
| `error` | 发生错误，message 字段有详情 |

凭证保存在 `.storage/auth/<platform>.json`（权限 600）。

---

## check — 检查登录态

```
mutipubcli check <platform>
```

只读查询当前登录态和账号信息，不修改任何文件。

```bash
mutipubcli check zhihu
```

输出包含：
- `status`: `authenticated` 或 `unauthenticated`
- `id` / `user_id`: 平台账号 ID
- `credentials_expire_at`: 凭证预计过期时间（有记录时）
- `credentials_updated_at`: 凭证最后更新时间

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
