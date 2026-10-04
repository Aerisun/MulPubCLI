# MulPubCLI

MulPubCLI 是一个多平台自动化文章发布命令行工具，当前支持小红书、知乎、今日头条、网易号和搜狐号。搜狐号 `publish` 直接提交图文公开投稿请求，提交后的审核状态可用 `verify` 回读。

主要特性：
- **按平台选择登录方式**：小红书与头条通过二维码接口登录，知乎按需启动浏览器扫码；发布流程由各平台客户端执行。
- **一致的体验**：为所有支持的平台提供完全统一的 CLI 接口结构与返回状态。
- **本地化存储**：所有凭证、账本和临时文件（如登录二维码）均统一收拢在项目级的 `.storage/` 文件夹下，权限严格受控。
- **可恢复的登录态**：`login` 复用已核验的有效凭证，二维码登录在同一命令中等待扫码结果；知乎跟随页面自动换码，扫码总等待时间上限为 2 分钟，小红书和头条二维码过期后重新执行即可换码。`session` 可实时核验登录态，`reset` 可清理后重来。
- **全自动图片处理**：文章发布时，能够自动从 Markdown 提取本地正文图片，并将封面与正文图自动上传转换，按需渲染不同平台的 HTML。

---

## 快速指南

### 1. 发布文章指南
了解如何准备你的图文 Markdown 稿件、封面图、以及内部图片处理机制：
👉 [发布图文与格式要求 (docs/publishing.md)](docs/publishing.md)

### 2. 命令行手册
查看完整的所有支持命令及选项，包含如何登录、检查状态和保存草稿：
👉 [CLI 接口手册与命令参考 (docs/cli.md)](docs/cli.md)

---

## 目录结构

```
MulPubCLI/
├── docs/                # 文档与手册
│   ├── cli.md
│   └── publishing.md
├── mulpubcli/          # 核心代码
│   ├── platforms/       # 各平台独立适配层
│   │   ├── xiaohongshu/
│   │   ├── zhihu/
│   │   ├── toutiao/
│   │   ├── netease/
│   │   └── sohu/
│   ├── core.py          # 文章数据模型与指纹算法
│   ├── http.py          # 基础 HTTP 封装与状态异常
│   ├── ledger.py        # 幂等发布账本控制
│   ├── renderer.py      # 通用 Markdown 到 HTML 与图片渲染层
│   ├── storage.py       # 集中化路径管理与资源存储 (.storage/)
│   └── __main__.py      # 统一的 CLI 执行入口
├── .storage/            # 运行时内部存储区 (gitignore 排除)
├── pyproject.toml
└── test_smoke.py
```
