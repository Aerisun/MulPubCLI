# 发布图文说明

本文档说明如何准备文章稿件、封面图和正文图，以及各平台发布图文的完整流程。

---

## 稿件格式

稿件为标准 Markdown 文件，规则如下：

- **第一行**必须是 `# 标题`，即 Markdown 一级标题
- 标题后空一行，其余为正文
- 封面用一行 HTML 注释指令 `<!-- cover: 路径 -->` 指定（标题行之后的任意行）
- 正文中可以用标准 Markdown 图片语法引用**本地图片**

```markdown
# 雾水之畔的木舟

<!-- cover: ./cover.jpg -->

清晨的湖面总是被一层薄薄的雾气笼罩着，仿佛天地间还没有完全醒来。

![晨雾配图](./images/morning.jpg)

柳枝在微风中轻轻摇曳，像是在诉说着昨夜未完的梦。
```

---

## 图片分类

| 类型 | 来源 | 说明 |
|------|------|------|
| **封面图** | 稿件内 `<!-- cover: 路径 -->` 指令 | 独立于正文，作为文章题图，必须提供 |
| **正文图** | 正文 `![alt](./local.jpg)` | 嵌在 Markdown 正文中的本地路径，自动上传 |

**规则：**
- 封面图和正文图均须为本地文件（JPG / PNG / WEBP）
- 正文中引用远程图片（`http://` / `https://`）直接保留，不上传
- 所有本地图片在 `mulpubcli publish` 执行时自动检查是否存在，缺失则立即报错，不会发出请求

---

## 图片上传流程

```
1. Article.load(article.md)
   ├─ 解析标题、定位封面指令（<!-- cover: -->）和正文
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
- 正文为图文结合笔记，封面图同时作为主图插入帖子顶部
- 图片格式：内部转换为 PNG，限制单图不超过 20MB

### 知乎 (zhihu)

- 封面图上传至 `api.zhihu.com/images`，经 OSS 二段式流程入库
- 封面以 `<figure><img>` 插在正文最前（`cover_first=True`）
- 正文图片逐一上传，URL 经知乎 CDN 替换后才稳定（最多等待约 30 秒）
- 图片格式：PNG / JPEG / WEBP，最大约 20MB

### 今日头条 (toutiao)

- 封面图上传至 `/spice/image`，返回 `uri`（身份标识）和 `url`
- 封面通过 `pgc_feed_covers` 字段单独传给接口，不插入正文 HTML（`cover_first=False`）
- 正文中的图片用标准 `<figure><img>` 渲染
- 图片格式：PNG / JPEG / WEBP

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
