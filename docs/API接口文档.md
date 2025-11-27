# JMComic API 接口文档

基于 `api_service/main.py` 的 FastAPI 服务，默认监听 `http://localhost:8000`。所有接口均返回 JSON，若请求错误会返回 HTTP 4xx/5xx 以及 `{"detail": "错误信息"}`。

> **说明**
>
> - 默认无需鉴权，可自行在 FastAPI 层添加 token / IP 白名单。
> - `JM_OPTION_PATH` 环境变量可指定使用的下载配置，未提供时使用默认配置。

---

## 配置文件：`api_config.yml`

项目根目录新增 `api_config.yml` 用于管理 API 服务。主要字段：

| 路径 | 说明 | 默认值 |
| --- | --- | --- |
| `server.host` / `server.port` | FastAPI 服务监听地址/端口 | `0.0.0.0 : 8000` |
| `download.root` | 下载与缓存根目录 | `./data` |
| `download.cache` | 是否复用已下载文件 | `true` |
| `download.image.decode` | 下载图片时是否解密 | `true` |
| `download.image.suffix` | 统一保存的图片后缀 | `.jpg` |
| `download.threading.image` | 下载图片的线程数（POST /images/download 使用） | `30` |
| `download.threading.photo` | 预留，控制章节级并发 | `16` |
| `logging.dir` | 日志目录（缺省时在 `download.root/logs`） | `null` |

可以通过环境变量 `JM_API_CONFIG` 指定其他配置文件；`JM_DOWNLOAD_DIR`、`JM_LOG_DIR` 仍可覆盖目录。
当服务运行时，日志会按日期划分目录保存在 `logging.dir/YYYY-MM-DD/api.log` 中。

---

## 1. 健康检查

- **Method / Path**：`GET /healthz`
- **用途**：检测容器/服务是否可用
- **响应**：
  ```json
  { "status": "ok" }
  ```

---

## 2. 搜索漫画列表

- **Method / Path**：`GET /api/albums`
- **参数**

| 名称 | 类型 | 说明 | 默认值 |
| --- | --- | --- | --- |
| `query` | `string` | 搜索关键词，支持 `+关键词` 必须包含、`-关键词` 排除 | `""` |
| `page` | `int` | 页码，1 起，最大 999 | `1` |
| `order_by` | `string` | 排序方式，参考 `JmMagicConstants.ORDER_BY_*` | `latest` |
| `time` | `string` | 时间范围，如 `all/week/month/day` | `all` |
| `category` | `string` | 分类，默认全部 | `all` |
| `sub_category` | `string` | 子分类，仅网页端支持，可空 | `null` |

- **分类值（category）说明**：详见"分类浏览"接口的分类值说明。常用值：`0`（全部）、`doujin`（同人）、`doujin_cosplay`（Cosplay）、`single`（单本）、`short`（短篇）、`another`（其他）、`hanman`（韩漫）、`meiman`（美漫）、`3D`、`english_site`（英文站）

- **响应字段**
  - `page` / `page_size` / `page_count` / `total`
  - `results[]`：`album_id`、`title`、`tags`、`authors`、`cover_url`、`view_count`、`like_count`、`last_update`
- **字段说明**
  - `results[].cover_url`：如禁漫列表未返回封面，则自动生成 `_3x4` 尺寸封面
  - `view_count` / `like_count`：沿用禁漫原始字符串（如 `40K`），未做数值化处理

---

## 3. 多维度搜索（作品/作者/标签/角色）

- **Method / Path**：
  - `GET /api/search/work`
  - `GET /api/search/author`
  - `GET /api/search/tag`
  - `GET /api/search/actor`
- **参数**：同 `/api/albums`
- **说明**：内部会把 `main_tag` 分别设置为 1（作品）、2（作者）、3（标签）、4（角色），返回结构与 `/api/albums` 一致。
- **响应字段**：同 `/api/albums`

---

## 4. 获取漫画详情

- **Method / Path**：`GET /api/albums/{album_id}`
- **参数**：`album_id`（路径参数，本子车号，可带/不带 `JM` 前缀）
- **响应字段**
  - `album_id`、`title`、`description`
  - `tags`、`authors`、`actors`、`works`
  - `likes`、`views`、`comment_count`
  - `pub_date`、`update_date`、`page_count`
  - `cover_url`
  - `chapters[]`：章节列表（见下一节）
- **字段说明**
  - `description`：禁漫简介的纯文本版本
  - `authors` / `actors` / `works`：均为字符串数组
  - `chapters`：包含章节基础信息，详细字段见下一节

---

## 5. 获取章节列表

- **Method / Path**：`GET /api/albums/{album_id}/chapters`
- **响应字段**
  - `album_id`、`title`
  - `chapters[]`
    - `order`：章节在禁漫中的排序值
    - `index`：数组顺序（从 1 开始）
    - `photo_id`：章节 ID
    - `title`：章节标题
    - `pub_date`：有些章节携带的发布日期
    - `thumbnail_url`：缩略图（复用本子封面，可按需扩展成章节首图）
- **补充说明**：若章节信息缺失，API 会依据禁漫返回的数据自动生成 `order`、`title` 等字段

---

## 6. 获取漫画封面

- **Method / Path**：`GET /api/albums/{album_id}/cover`
- **说明**：获取漫画封面，如果本地不存在则自动下载到本地并转换为 JPG 格式
- **响应字段**：
  - `album_id`：漫画 ID
  - `cover_url`：封面的公开访问 URL（`/downloads/photos/{album_id}/cover.jpg`）
  - `cover_path`：封面的相对路径（`photos/{album_id}/cover.jpg`，相对于下载根目录）
- **存储路径**：`{DOWNLOAD_ROOT}/photos/{album_id}/cover.jpg`
- **行为**：
  - 如果本地已存在且缓存启用，直接返回本地路径
  - 如果本地不存在或缓存禁用，自动下载并转换为 JPG 格式
  - 下载的封面会保存在与章节图片相同的目录结构下（`photos/{album_id}/`）
- **示例响应**：
  ```json
  {
    "album_id": "1230497",
    "cover_url": "/downloads/photos/1230497/cover.jpg",
    "cover_path": "photos/1230497/cover.jpg"
  }
  ```

---

## 6.1. 删除漫画封面

- **Method / Path**：`DELETE /api/albums/{album_id}/cover`
- **说明**：删除已下载的漫画封面图片
- **响应字段**：
  - `album_id`：漫画 ID
  - `removed`：是否成功删除（`true`/`false`）
- **示例响应**：
  ```json
  {
    "album_id": "1230497",
    "removed": true
  }
  ```
- **注意**：删除操作只会删除封面文件，不会删除目录结构

---

## 7. 获取章节详情

- **Method / Path**：`GET /api/photos/{photo_id}`
- **参数**：`photo_id`（章节 ID，可带/不带 `JM` 前缀）
- **响应字段**
  - `photo_id`、`album_id`
  - `title`
  - `order`：在所属本子中的排序值
  - `page_count`
  - `tags`
  - `scramble_id`：图片解密使用的 scramble 标识
- **补充说明**：接口会尝试补齐 `page_arr`、`data_original_domain` 等信息，以便后续图片下载

---

## 8. 获取章节图片列表

- **Method / Path**：`GET /api/photos/{photo_id}/images`
- **响应字段**
  - `photo_id`、`album_id`、`title`
  - `images[]`
    - `index`：图片序号（1 起）
    - `download_url`：原图下载地址（携带 `?v=` 参数）
    - `filename`：图片文件名（含后缀）
    - `scramble_id`：用于解密图片的 ID
- **字段说明**
  - `images[].download_url`：直接指向禁漫 CDN，未经过 API 服务缓存
  - 若只需第一张图片作为缩略图，可直接取 `images[0]`

> **提示**
>
> - `download_url` 可直接配合 `curl`/浏览器下载；若需要解密，可使用 `JmImageTool.decode_and_save` 或调用原项目已有插件。
> - 若需仅获取第一张图片作为章节缩略图，可在后端调用 `GET /api/photos/{photo_id}/images` 后取 `images[0]`。

---

## 9. 分类浏览

- **Method / Path**：`GET /api/categories`
- **参数**

| 名称 | 类型 | 说明 | 默认值 |
| --- | --- | --- | --- |
| `page` | `int` | 页码 | `1` |
| `time` | `string` | 时间范围（`all/week/month/day` 等） | `all` |
| `category` | `string` | 分类 | `all` |
| `order_by` | `string` | 排序方式 | `latest` |
| `sub_category` | `string` | 子分类 | `null` |

- **分类值（category）说明**：
  - `0` 或 `all`：全部
  - `doujin`：同人
  - `single`：单本
  - `short`：短篇
  - `another`：其他
  - `hanman`：韩漫
  - `meiman`：美漫
  - `doujin_cosplay`：**Cosplay（注意：不是 `cosplay`）**
  - `3D`：3D
  - `english_site`：英文站

- **副分类值（sub_category）说明**（仅部分分类支持）：
  - 通用：`chinese`（汉化）、`japanese`（日语）
  - 其他类（`category=another`）：`other`、`3d`、`cosplay`
  - 同人类（`category=doujin`）：`CG`、`chinese`、`japanese`
  - 短篇类（`category=short`）：`chinese`、`japanese`
  - 单本类（`category=single`）：`chinese`、`japanese`、`youth`

- **响应**：同 `/api/albums`，返回一个 `results[]` 列表。
- **字段说明**：`page_count` 基于 `JmModuleConfig.PAGE_SIZE_SEARCH` 计算，`results` 数据结构与搜索接口一致

- **示例**：
  - 获取 Cosplay 分类：`GET /api/categories?category=doujin_cosplay`
  - 获取"其他"分类下的 Cosplay 子分类：`GET /api/categories?category=another&sub_category=cosplay`
  
- **注意**：如果 `category=doujin_cosplay` 不起作用，可以尝试以下替代方案：
  1. **推荐**：使用专门的 Cosplay 接口：`GET /api/categories/cosplay`（会自动尝试分类接口，失败则使用标签搜索）
  2. 使用搜索接口按标签搜索：`GET /api/search/tag?query=cosplay&page=1`
  3. 使用"其他"分类的子分类：`GET /api/categories?category=another&sub_category=cosplay`
  4. 使用站内搜索：`GET /api/albums?query=+cosplay&page=1`

---

## 9.1. 获取 Cosplay 分类（专用接口）

- **Method / Path**：`GET /api/categories/cosplay`
- **说明**：专门用于获取 Cosplay 分类数据的接口，会自动尝试多种方式获取数据
- **参数**：

| 名称 | 类型 | 说明 | 默认值 |
| --- | --- | --- | --- |
| `page` | `int` | 页码 | `1` |
| `time` | `string` | 时间范围（`all/week/month/day` 等） | `all` |
| `order_by` | `string` | 排序方式 | `latest` |

- **工作流程**：
  1. 首先尝试使用分类接口：`category=doujin_cosplay`
  2. 如果失败或返回空结果，自动切换到标签搜索：`search_tag('cosplay')`
  3. 返回搜索结果

- **响应**：同 `/api/albums`，返回一个 `results[]` 列表
- **优势**：自动处理分类接口可能不支持的情况，确保能获取到 Cosplay 相关数据

---

## 10. 排行榜

- **Method / Path**：`GET /api/rankings/{scope}`
- **路径参数**：`scope` 取值 `month/week/day`
- **查询参数**：

| 名称 | 类型 | 说明 | 默认值 |
| --- | --- | --- | --- |
| `page` | `int` | 页码 | `1` |
| `category` | `string` | 分类 | `all` |

- **响应**：同 `/api/albums`，分别对应月榜、周榜、日榜（底层等价于分类接口 + 固定时间/排序）。
- **字段说明**：`scope=month/week/day` 分别等价于时间维度 `TIME_MONTH/TIME_WEEK/TIME_TODAY`，排序统一为观看数（`ORDER_BY_VIEW`）

---

## 11. 下载漫画缩略图

- **Method / Path**：`POST /api/albums/{album_id}/thumbnail`
- **说明**：下载并缓存指定漫画的封面图，统一转换为 JPG，保存在 `JM_DOWNLOAD_DIR/thumbnails/{album_id}.jpg`。
- **响应**：`{ "album_id": "...", "thumbnail_url": "/downloads/thumbnails/{album_id}.jpg" }`
- **附加字段**：`thumbnail_path` 为磁盘绝对路径，便于自定义处理。
- **备注**：如文件已存在则直接返回缓存，不会重复下载。

---

## 12. 下载章节图片

- **Method / Path**：`POST /api/photos/{photo_id}/images/download`
- **行为**：
  - 自动获取章节所有图片，逐张解密并转换为 JPG。
  - 统一存储在 `JM_DOWNLOAD_DIR/photos/{album_id}/{photo_id}/xxxxx.jpg`。
  - 如果该目录下已有所有 JPG 文件，则直接返回缓存。
- **响应**：
  ```json
  {
    "photo_id": "...",
    "album_id": "...",
    "count": 20,
    "images": [
      {
        "url": "/downloads/photos/{album}/{photo}/00001.jpg",
        "path": "D:/.../photos/{album}/{photo}/00001.jpg"
      }
    ]
  }
  ```
- **提示**：`/downloads/...` 为本地静态文件映射，可通过 `http://host:8000/downloads/...` 直接访问。
- **并发**：实际下载线程数由 `download.threading.image` 控制，可在 `api_config.yml` 调整。

---

## 13. 删除章节图片

- **Method / Path**：`DELETE /api/photos/{photo_id}/images`
- **说明**：删除已下载的 JPG 文件但保留目录，便于后续重新下载。
- **响应**：
  ```json
  {
    "photo_id": "...",
    "album_id": "...",
    "deleted_files": ["00001.jpg", "00002.jpg"]
  }
  ```

---

## 14. 删除漫画缩略图

- **Method / Path**：`DELETE /api/albums/{album_id}/thumbnail`
- **说明**：删除 `thumbnails/{album_id}.jpg` 文件但保留目录，便于重新拉取。
- **响应**：
  ```json
  {
    "album_id": "...",
    "removed": true
  }
  ```

---

## 常见错误码

| HTTP 状态码 | 说明 |
| --- | --- |
| `400` | 参数不合法或禁漫返回异常 |
| `404` | 找不到对应的本子/章节 |
| `500` | 内部错误（FastAPI 或依赖库异常） |

如需扩展上传、任务队列、鉴权等功能，可在 `api_service` 中继续添加 Router 或依赖。若要生成自动化文档，可访问 `http://localhost:8000/docs` 查看 OpenAPI/Swagger 页面。***

