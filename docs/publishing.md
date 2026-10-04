# 发布图文说明

本文档说明如何准备文章稿件、封面图和正文图，以及各平台发布图文的完整流程。

---

## 稿件格式

稿件为标准 Markdown 文件，规则如下：

- **第一行**必须是 `# 标题`，即 Markdown 一级标题
- 标题后空一行，其余为正文
- 封面用一行 HTML 注释指令 `<!-- cover: 路径 -->` 指定（标题行之后的任意行）
- 摘要可用 `<!-- summary: 摘要内容 -->` 指定；也支持在注释内换行
- 正文中可以用标准 Markdown 图片语法引用**本地图片**

```markdown
# 雾水之畔的木舟

<!-- cover: ./cover.jpg -->

<!-- summary: 湖面晨雾与木舟的故事。 -->

清晨的湖面总是被一层薄薄的雾气笼罩着，仿佛天地间还没有完全醒来。

![晨雾配图](./images/morning.jpg)

柳枝在微风中轻轻摇曳，像是在诉说着昨夜未完的梦。
```

标题、封面指令和摘要指令在加载时分别提取，不进入正文。封面和摘要指令各只能出现一次；格式不完整会停止发布，避免把元数据写入正文。

### 摘要提交范围

| 渠道 | 当前发布路径的摘要处理 |
|------|------------------------|
| 小红书 | 当前普通图文请求用 `common.desc` 提交正文，没有独立摘要参数；丢弃摘要。创作者前端另有长文封面的“摘要”文字设置，当前普通图文路径不使用 |
| 知乎 | 当前文章草稿及发表请求未发现独立摘要参数；丢弃摘要 |
| 今日头条 | 提交到 `search_creation_info.abstract` |
| 网易号 | 当前 HTTP 草稿请求及 v4 编辑器的 `publishV2.do` 参数均未发现独立摘要参数；丢弃摘要 |
| 搜狐号 | 图文直接投稿时提交到 `brief`；`publish sohu` 调用公开投稿接口 |

摘要不会自动拼到任何渠道的正文中。
上述结论只针对本项目接入的发布类型及请求。小红书长文封面摘要不是普通图文的独立摘要提交字段。

---

## 图片分类

| 类型 | 来源 | 说明 |
|------|------|------|
| **封面图** | 稿件内 `<!-- cover: 路径 -->` 指令 | 文章题图，必须提供；网易号还会把它放在正文首图，小红书把它作为图集首图 |
| **正文图** | 正文 `![alt](./local.jpg)` | 嵌在 Markdown 正文中的本地路径，自动上传 |

**规则：**
- 封面图和正文图均须为本地文件（JPG / PNG / WEBP）
- 知乎、头条、网易 HTTP 草稿和搜狐的 HTML 正文可以保留远程图片 URL；小红书图集与网易浏览器公开发布只支持本地图片，遇到远程图片会在提交前报错
- 所有本地图片在 `mulpubcli publish` 执行时自动检查是否存在，缺失则立即报错，不会发出请求

---

## 图片上传流程

```
1. Article.load(article.md)
   ├─ 解析标题、封面和摘要指令，从正文中移除
   └─ 扫描正文中的本地图片路径 → body_images

2. 上传封面图
   cover.jpg → 平台 CDN → cover_url

3. 上传正文图片（按顺序）
   ./images/morning.jpg → 平台 CDN → body_img_url_1
   ...

4. 渲染 HTML
   image_map = {
     str(cover.jpg):              cover_url,
     str(images/morning.jpg):     body_img_url_1,
   }
   html = render(article, image_map, cover_first=..., include_title=False)

5. POST 到平台接口（带标题、HTML 正文、封面元数据）
```

任一正文图片上传失败，发布立即中止，不会提交文章。

---

## 各平台图片处理差异

### 小红书 (xiaohongshu)

- 封面图经 PNG 转换后上传至小红书内容中台
- 正文文字与图片分别提交为 `desc` 和按原稿顺序排列的图集；平台当前接口没有文字与图片逐段穿插的结构
- 图片格式：内部转换为 PNG，限制单图不超过 20MB

### 知乎 (zhihu)

- 封面图上传至 `api.zhihu.com/images`，经 OSS 二段式流程入库
- 封面通过草稿的 `titleImage` 字段单独提交并回读；正文仅保留 Markdown 中引用的图片
- 正文图片逐一上传，URL 经知乎 CDN 替换后才稳定（最多等待约 30 秒）
- 图片格式：PNG / JPEG / WEBP，最大约 20MB

### 今日头条 (toutiao)

- 封面图上传至 `/spice/image`，返回 `uri`（身份标识）和 `url`
- 封面写入 `pgc_feed_covers`，不自动插入正文 HTML
- 正文中的图片用标准 `<figure><img>` 渲染
- 图片格式：PNG / JPEG / WEBP

### 网易号 (netease)

- 网易号是文章渠道的例外：封面同时作为正文第一张图，其余正文图按 Markdown 原始位置穿插
- HTTP 草稿把封面写入 `cover` 字段，并将封面放在正文 HTML 开头
- 公开发布走浏览器编辑器，先插入封面，再交替粘贴正文文字和图片；从正文图片中选择第一张作为封面
- 若图片未插入，或编辑器最终图文顺序与原稿及封面开头规则不一致，提交前停止
- 浏览器路径按图片扩展名设置 JPEG / PNG / WEBP 媒体类型

### 搜狐号 (sohu)

- 封面写入 `cover` 字段；正文只按 Markdown 位置嵌入正文图片
- `publish sohu` 直接调用图文公开投稿接口；摘要随请求的 `brief` 字段提交
- 平台审核中的文章先返回 `pending`，可用 `verify --platform sohu` 回读

---

## 推荐目录结构

```
my-article/
├── article.md          # 稿件（第一行为标题）
├── cover.jpg           # 封面图
└── images/
    ├── morning.jpg     # 正文图 1（在 article.md 中引用）
    └── scene.jpg       # 正文图 2
```

发布命令：

```bash
mulpubcli publish zhihu \
  --article my-article/article.md
```
