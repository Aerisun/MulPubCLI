# MulPubCLI

MulPubCLI 是多平台文章发布命令行工具，几乎全部使用 HTTP 内核完成（只是在必要处短暂调用浏览器），支持小红书、知乎、今日头条、网易号和搜狐号。

## 快速开始

```bash
pip install -e .
mulpubcli login zhihu
mulpubcli publish zhihu --article article.md
mulpubcli verify --platform zhihu
mulpubcli list 
```

## 文档

- [CLI 手册](docs/cli.md)：安装、登录、发布、草稿、列表和核验命令。
- [图文发布说明](docs/publishing.md)：稿件格式、图片上传及各平台处理方式。

## 项目结构

```text
.
├── mulpubcli/                 # Python 包
│   ├── __main__.py            # CLI 命令入口
│   ├── core.py                # 文章模型与内容指纹
│   ├── http.py                # HTTP 会话与请求封装
│   ├── browser.py             # 按需启动的浏览器登录组件
│   ├── ledger.py              # 发布记录与重复提交控制
│   ├── renderer.py            # Markdown 与图片渲染
│   ├── storage.py             # 本地数据路径管理
│   └── platforms/             # 各平台登录、发布与核验实现
│       ├── xiaohongshu/
│       ├── zhihu/
│       ├── toutiao/
│       ├── netease/
│       └── sohu/
├── docs/
│   ├── cli.md                 # 命令与参数说明
│   └── publishing.md          # 稿件格式与图文发布说明
├── tests/                     # 自动化测试
├── .storage/                  # 本地运行数据，已被 Git 忽略
│   ├── auth/                  # 登录凭据与浏览器配置
│   ├── qr/                    # 登录二维码
│   ├── results/               # 发布记录
│   └── tmp/                   # 运行时临时文件
├── pyproject.toml             # 安装配置与依赖
└── README.md
```

