# MulPubCLI

MulPubCLI 是多平台文章发布命令行工具，支持小红书、知乎、今日头条、网易号和搜狐号。各平台的登录与发布能力略有差异，具体命令以 [CLI 手册](docs/cli.md)为准。

## 快速开始

```bash
pip install -e .
mulpubcli login zhihu
mulpubcli publish zhihu --article article.md
mulpubcli verify --platform zhihu
```

稿件第一行是 `# 标题`，封面用 `<!-- cover: ./cover.jpg -->` 指定。图片格式、摘要处理及平台差异见[图文发布说明](docs/publishing.md)。也可以用 `python -m mulpubcli` 运行命令。

## 文档

- [CLI 手册](docs/cli.md)：安装、登录、发布、草稿、列表和核验命令。
- [图文发布说明](docs/publishing.md)：稿件格式、图片上传及各平台处理方式。

## 本地数据

运行时凭据、浏览器配置、二维码和发布记录放在项目根目录的 `.storage/`，该目录已被 Git 忽略。登录凭据和浏览器配置在 `.storage/auth/`；手工导出的 Cookie 文件也应放在这里，并限制文件权限。项目根目录的 `auto/` 和 `ck.txt` 另有忽略规则，防止误提交；它们现在分别存放于 `.storage/auth/auto/` 和 `.storage/auth/ck.txt`。这两份手工数据不会被 CLI 自动读取，使用它们的外部命令需要指向新路径。详见 [CLI 手册中的内部存储结构](docs/cli.md#内部存储结构)。

## 项目结构

```text
mulpubcli/      命令行入口、存储、渲染和各平台适配代码
docs/           CLI 手册与图文发布说明
tests/          自动化测试
.storage/       本地运行时数据（不入库）
pyproject.toml  安装配置与依赖
```
