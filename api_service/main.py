import atexit
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

import threading

import yaml
from fastapi import Depends, FastAPI, HTTPException, Path as FastAPIPath, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from PIL import Image

from jmcomic import (
    JmAlbumDetail,
    JmMagicConstants,
    JmOption,
    JmPhotoDetail,
    JmSearchPage,
    JmcomicText,
    create_option_by_file,
)

DEFAULT_CONFIG = {
    "server": {"host": "0.0.0.0", "port": 8000},
    "download": {
        "root": "./data",
        "cache": True,
        "image": {"decode": True, "suffix": ".jpg"},
        "threading": {"image": 30, "photo": 16},
    },
    "logging": {"dir": None},
}


def load_api_config() -> Dict:
    config_path = Path(os.getenv("JM_API_CONFIG", Path.cwd() / "api_config.yml"))
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as fp:
            data = yaml.safe_load(fp) or {}
    else:
        data = {}
    return deep_merge(DEFAULT_CONFIG, data)


def deep_merge(base: Dict, override: Dict) -> Dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


CONFIG = load_api_config()


def resolve_path(path_str: Optional[str], fallback: Path) -> Path:
    if path_str is None:
        return fallback.resolve()
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


DOWNLOAD_ROOT = resolve_path(CONFIG["download"].get("root", "./data"), Path.cwd() / "data")
PHOTO_ROOT = DOWNLOAD_ROOT / "photos"
THUMB_ROOT = DOWNLOAD_ROOT / "thumbnails"
LOG_ROOT = resolve_path(CONFIG.get("logging", {}).get("dir"), DOWNLOAD_ROOT / "logs")
os.environ.setdefault("JM_DOWNLOAD_DIR", str(DOWNLOAD_ROOT))
os.environ.setdefault("JM_LOG_DIR", str(LOG_ROOT))

CACHE_ENABLED = bool(CONFIG["download"].get("cache", True))
IMAGE_OPTIONS = CONFIG["download"].get("image", {})
THREADING_OPTIONS = CONFIG["download"].get("threading", {})
DECODE_IMAGES = bool(IMAGE_OPTIONS.get("decode", True))
FINAL_SUFFIX = IMAGE_OPTIONS.get("suffix") or ".jpg"
if not FINAL_SUFFIX.startswith("."):
    FINAL_SUFFIX = f".{FINAL_SUFFIX}"
IMAGE_THREAD_COUNT = max(1, int(THREADING_OPTIONS.get("image", 1)))
_PHOTO_THREAD_COUNT = max(1, int(THREADING_OPTIONS.get("photo", 1)))


class DailyFileHandler(logging.Handler):
    def __init__(self, log_root: Path, prefix: str = "api"):
        super().__init__()
        self.log_root = Path(log_root)
        self.prefix = prefix
        self.current_date: Optional[str] = None
        self.stream = None
        self.lock = threading.RLock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            with self.lock:
                self._ensure_stream()
                self.stream.write(msg + "\n")
                self.stream.flush()
        except Exception:
            self.handleError(record)

    def _ensure_stream(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self.stream and self.current_date == today:
            return
        self._close_stream()
        daily_dir = self.log_root / today
        daily_dir.mkdir(parents=True, exist_ok=True)
        file_path = daily_dir / f"{self.prefix}.log"
        self.stream = open(file_path, "a", encoding="utf-8")
        self.current_date = today

    def _close_stream(self):
        if self.stream:
            try:
                self.stream.close()
            finally:
                self.stream = None

    def close(self):
        with self.lock:
            self._close_stream()
        super().close()


def setup_logger() -> logging.Logger:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("jmcomic_api")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    handler = DailyFileHandler(LOG_ROOT)
    formatter = logging.Formatter(
        fmt="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    atexit.register(handler.close)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    return logger


logger = setup_logger()


class OptionProvider:
    """
    Lazily loads a JmOption instance so we can reuse configuration across requests.
    """

    def __init__(self) -> None:
        self._option: Optional[JmOption] = None

    def load(self) -> JmOption:
        option_path = os.getenv("JM_OPTION_PATH")
        if option_path:
            return create_option_by_file(option_path)
        return JmOption.default()

    def get(self) -> JmOption:
        if self._option is None:
            self._option = self.load()
        return self._option


option_provider = OptionProvider()


def get_option() -> JmOption:
    return option_provider.get()


@lru_cache()
def get_app() -> FastAPI:
    app = FastAPI(title="JMComic API Service")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    app.mount("/downloads", StaticFiles(directory=str(DOWNLOAD_ROOT)), name="downloads")

    register_routes(app)

    @app.middleware("http")
    async def log_requests(request, call_next):
        start = time.time()
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            duration = (time.time() - start) * 1000
            logger.info(
                "HTTP %s %s %s %.2fms",
                request.method,
                request.url.path,
                response.status_code if response else "NA",
                duration,
            )
    return app


def register_routes(app: FastAPI) -> None:
    @app.get("/healthz")
    def health_check():
        return {"status": "ok"}

    @app.get("/api/albums")
    def list_albums(
        query: str = Query("", description="搜索关键词，支持 +关键字/-关键字"),
        page: int = Query(1, ge=1, le=999),
        order_by: str = Query(JmMagicConstants.ORDER_BY_LATEST),
        time: str = Query(JmMagicConstants.TIME_ALL),
        category: str = Query(JmMagicConstants.CATEGORY_ALL),
        sub_category: Optional[str] = Query(None),
        option: JmOption = Depends(get_option),
    ):
        client = option.build_jm_client()
        return execute_site_search(
            client=client,
            query=query,
            page=page,
            order_by=order_by,
            time=time,
            category=category,
            sub_category=sub_category,
        )

    def build_search_endpoint(route: str, main_tag: int):
        @app.get(route)
        def multi_search(
            query: str = Query("", description="搜索关键词，支持 +关键字/-关键字"),
            page: int = Query(1, ge=1, le=999),
            order_by: str = Query(JmMagicConstants.ORDER_BY_LATEST),
            time: str = Query(JmMagicConstants.TIME_ALL),
            category: str = Query(JmMagicConstants.CATEGORY_ALL),
            sub_category: Optional[str] = Query(None),
            option: JmOption = Depends(get_option),
        ):
            client = option.build_jm_client()
            return execute_generic_search(
                client=client,
                main_tag=main_tag,
                query=query,
                page=page,
                order_by=order_by,
                time=time,
                category=category,
                sub_category=sub_category,
            )

        return multi_search

    build_search_endpoint("/api/search/work", 1)
    build_search_endpoint("/api/search/author", 2)
    build_search_endpoint("/api/search/tag", 3)
    build_search_endpoint("/api/search/actor", 4)

    @app.get("/api/albums/{album_id}")
    def album_detail(
        album_id: str,
        option: JmOption = Depends(get_option),
    ):
        client = option.build_jm_client()
        album = fetch_album_detail(client, album_id)
        return serialize_album_detail(album)

    @app.get("/api/albums/{album_id}/chapters")
    def album_chapters(
        album_id: str,
        option: JmOption = Depends(get_option),
    ):
        client = option.build_jm_client()
        album = fetch_album_detail(client, album_id)
        return {
            "album_id": album.album_id,
            "title": album.name,
            "chapters": serialize_album_chapters(album),
        }

    @app.get("/api/albums/{album_id}/cover")
    def album_cover(
        album_id: str,
        option: JmOption = Depends(get_option),
    ):
        """获取漫画封面，如果本地不存在则下载"""
        client = option.build_jm_client()
        cover_path = ensure_album_cover(album_id, client)
        logger.info("cover.ready album=%s path=%s", album_id, cover_path)
        return {
            "album_id": str(album_id),
            "cover_url": to_public_url(cover_path),
            "cover_path": to_relative_path(cover_path),
        }

    @app.delete("/api/albums/{album_id}/cover")
    def delete_album_cover(
        album_id: str,
    ):
        """删除漫画封面图片"""
        cover_path = get_album_cover_path(album_id)
        removed = False
        if cover_path.exists():
            cover_path.unlink(missing_ok=True)
            removed = True
        logger.info("cover.deleted album=%s removed=%s", album_id, removed)
        return {
            "album_id": album_id,
            "removed": removed,
        }

    @app.get("/api/photos/{photo_id}")
    def photo_detail(
        photo_id: str,
        option: JmOption = Depends(get_option),
    ):
        client = option.build_jm_client()
        photo = fetch_photo_detail(client, photo_id)
        return serialize_photo_detail(photo)

    @app.get("/api/photos/{photo_id}/images")
    def photo_images(
        photo_id: str,
        option: JmOption = Depends(get_option),
    ):
        client = option.build_jm_client()
        photo = fetch_photo_detail(client, photo_id)
        return {
            "photo_id": photo.photo_id,
            "album_id": photo.album_id,
            "title": photo.name,
            "images": serialize_photo_images(photo),
        }

    @app.post("/api/albums/{album_id}/thumbnail")
    def download_album_thumbnail(
        album_id: str,
        option: JmOption = Depends(get_option),
    ):
        client = option.build_jm_client()
        album = fetch_album_detail(client, album_id)
        thumbnail_path = ensure_thumbnail(album.album_id, client)
        logger.info("thumbnail.ready album=%s path=%s", album.album_id, thumbnail_path)
        return {
            "album_id": album.album_id,
            "thumbnail_url": to_public_url(thumbnail_path),
            "thumbnail_path": to_relative_path(thumbnail_path),
        }

    @app.delete("/api/albums/{album_id}/thumbnail")
    def delete_album_thumbnail(
        album_id: str,
    ):
        thumbnail_path = THUMB_ROOT / f"{album_id}.jpg"
        removed = False
        if thumbnail_path.exists():
            thumbnail_path.unlink(missing_ok=True)
            removed = True
        logger.info("thumbnail.deleted album=%s removed=%s", album_id, removed)
        return {
            "album_id": album_id,
            "removed": removed,
        }

    @app.post("/api/photos/{photo_id}/images/download")
    def download_photo_images_endpoint(
        photo_id: str,
        option: JmOption = Depends(get_option),
    ):
        client = option.build_jm_client()
        photo = fetch_photo_detail(client, photo_id)
        image_urls = ensure_photo_images(photo, client)
        logger.info(
            "photo.images.ready album=%s photo=%s count=%d",
            photo.album_id,
            photo.photo_id,
            len(image_urls),
        )
        return {
            "photo_id": photo.photo_id,
            "album_id": photo.album_id,
            "count": len(image_urls),
            "images": [
                {"url": to_public_url(path), "path": to_relative_path(path)}
                for path in image_urls
            ],
        }

    @app.delete("/api/photos/{photo_id}/images")
    def delete_photo_images(
        photo_id: str,
        option: JmOption = Depends(get_option),
    ):
        client = option.build_jm_client()
        photo = fetch_photo_detail(client, photo_id)
        deleted = clear_photo_images(photo)
        logger.info(
            "photo.images.deleted album=%s photo=%s deleted=%d",
            photo.album_id,
            photo.photo_id,
            len(deleted),
        )
        return {
            "photo_id": photo.photo_id,
            "album_id": photo.album_id,
            "deleted_files": deleted,
        }

    @app.get("/api/categories")
    def categories(
        page: int = Query(1, ge=1),
        time: str = Query(JmMagicConstants.TIME_ALL),
        category: str = Query(JmMagicConstants.CATEGORY_ALL),
        order_by: str = Query(JmMagicConstants.ORDER_BY_LATEST),
        sub_category: Optional[str] = Query(None),
        option: JmOption = Depends(get_option),
    ):
        client = option.build_jm_client()
        try:
            logger.info(
                "categories.filter page=%d category=%s sub_category=%s time=%s order_by=%s client_type=%s",
                page,
                category,
                sub_category,
                time,
                order_by,
                client.client_key if hasattr(client, 'client_key') else 'unknown',
            )
            page_content = client.categories_filter(
                page=page,
                time=time,
                category=category,
                order_by=order_by,
                sub_category=sub_category,
            )
            logger.info(
                "categories.filter.success page=%d category=%s total=%d results=%d",
                page,
                category,
                page_content.total if hasattr(page_content, 'total') else 0,
                len(page_content) if hasattr(page_content, '__len__') else 0,
            )
        except Exception as exc:
            logger.error(
                "categories.filter.failed page=%d category=%s sub_category=%s error=%s",
                page,
                category,
                sub_category,
                str(exc),
                exc_info=True,
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return serialize_search_page(page_content, page)

    @app.get("/api/categories/cosplay")
    def categories_cosplay(
        page: int = Query(1, ge=1),
        time: str = Query(JmMagicConstants.TIME_ALL),
        order_by: str = Query(JmMagicConstants.ORDER_BY_LATEST),
        option: JmOption = Depends(get_option),
    ):
        """
        专门用于获取 Cosplay 分类的接口
        优先尝试使用分类接口，如果失败则使用标签搜索作为备选方案
        """
        client = option.build_jm_client()
        
        # 方案1: 尝试使用分类接口
        try:
            logger.info("categories.cosplay.try_filter page=%d", page)
            page_content = client.categories_filter(
                page=page,
                time=time,
                category=JmMagicConstants.CATEGORY_DOUJIN_COSPLAY,
                order_by=order_by,
                sub_category=None,
            )
            if page_content and hasattr(page_content, 'total') and page_content.total > 0:
                logger.info("categories.cosplay.filter.success total=%d", page_content.total)
                return serialize_search_page(page_content, page)
        except Exception as exc:
            logger.warning("categories.cosplay.filter.failed error=%s, fallback to search", str(exc))
        
        # 方案2: 使用标签搜索作为备选
        try:
            logger.info("categories.cosplay.try_search page=%d", page)
            page_content = client.search_tag(
                search_query='cosplay',
                page=page,
                order_by=order_by,
                time=time,
                category=JmMagicConstants.CATEGORY_ALL,
                sub_category=None,
            )
            logger.info("categories.cosplay.search.success total=%d", page_content.total if hasattr(page_content, 'total') else 0)
            return serialize_search_page(page_content, page)
        except Exception as exc:
            logger.error("categories.cosplay.search.failed error=%s", str(exc), exc_info=True)
            raise HTTPException(status_code=400, detail=f"获取 Cosplay 数据失败: {exc}") from exc

    @app.get("/api/rankings/{scope}")
    def rankings(
        scope: str = FastAPIPath(..., pattern="^(month|week|day)$"),
        page: int = Query(1, ge=1),
        category: str = Query(JmMagicConstants.CATEGORY_ALL),
        option: JmOption = Depends(get_option),
    ):
        client = option.build_jm_client()
        try:
            if scope == "month":
                page_content = client.month_ranking(page=page, category=category)
            elif scope == "week":
                page_content = client.week_ranking(page=page, category=category)
            else:
                page_content = client.day_ranking(page=page, category=category)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return serialize_search_page(page_content, page)


def serialize_search_page(page: JmSearchPage, current_page: int) -> Dict:
    return {
        "page": current_page,
        "page_size": page.page_size,
        "page_count": page.page_count,
        "total": page.total,
        "results": [
            serialize_album_item(album_id, info) for album_id, info in page.content
        ],
    }


def serialize_album_item(album_id: str, info: Dict) -> Dict:
    return {
        "album_id": album_id,
        "title": info.get("name"),
        "tags": info.get("tags") or [],
        "authors": info.get("author") or info.get("authors"),
        "category": info.get("category"),
        "cover_url": info.get("cover")
        or info.get("image")
        or JmcomicText.get_album_cover_url(album_id, size="_3x4"),
        "view_count": info.get("views"),
        "like_count": info.get("likes"),
        "last_update": info.get("update_date"),
    }


def serialize_album_detail(album: JmAlbumDetail) -> Dict:
    return {
        "album_id": album.album_id,
        "title": album.name,
        "description": album.description,
        "tags": album.tags,
        "authors": album.authors,
        "actors": album.actors,
        "works": album.works,
        "likes": album.likes,
        "views": album.views,
        "comment_count": album.comment_count,
        "pub_date": album.pub_date,
        "update_date": album.update_date,
        "page_count": album.page_count,
        "cover_url": JmcomicText.get_album_cover_url(album.album_id),
        "chapters": serialize_album_chapters(album),
    }


def serialize_album_chapters(album: JmAlbumDetail) -> List[Dict]:
    chapters = []
    for index, episode in enumerate(album.episode_list, start=1):
        photo_id = episode[0]
        order = int(episode[1]) if len(episode) >= 2 else index
        title = episode[2] if len(episode) >= 3 else f"第{order}话"
        pub_date = episode[3] if len(episode) >= 4 else None
        chapters.append(
            {
                "order": order,
                "index": index,
                "photo_id": photo_id,
                "title": title,
                "pub_date": pub_date,
                "thumbnail_url": JmcomicText.get_album_cover_url(album.album_id, size="_3x4"),
            }
        )
    return chapters


def serialize_photo_detail(photo: JmPhotoDetail) -> Dict:
    return {
        "photo_id": photo.photo_id,
        "album_id": photo.album_id,
        "title": photo.name,
        "order": photo.album_index,
        "page_count": len(photo.page_arr or []),
        "tags": photo.tags,
        "scramble_id": photo.scramble_id,
    }


def serialize_photo_images(photo: JmPhotoDetail) -> List[Dict]:
    images: List[Dict] = []
    total = len(photo.page_arr or [])
    for idx in range(total):
        image = photo.create_image_detail(idx)
        images.append(
            {
                "index": idx + 1,
                "download_url": image.download_url,
                "filename": image.filename,
                "scramble_id": image.scramble_id,
            }
        )
    return images


def fetch_album_detail(client, album_id: str) -> JmAlbumDetail:
    try:
        return client.get_album_detail(album_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"album {album_id} not found: {exc}",
        ) from exc


def fetch_photo_detail(client, photo_id: str) -> JmPhotoDetail:
    try:
        photo = client.get_photo_detail(photo_id)
        client.check_photo(photo)
        return photo
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"photo {photo_id} not found: {exc}",
        ) from exc


def execute_site_search(
    client,
    query: str,
    page: int,
    order_by: str,
    time: str,
    category: str,
    sub_category: Optional[str],
):
    return execute_generic_search(
        client=client,
        main_tag=0,
        query=query,
        page=page,
        order_by=order_by,
        time=time,
        category=category,
        sub_category=sub_category,
    )


def execute_generic_search(
    client,
    main_tag: int,
    query: str,
    page: int,
    order_by: str,
    time: str,
    category: str,
    sub_category: Optional[str],
):
    try:
        search_page = client.search(
            search_query=query,
            page=page,
            main_tag=main_tag,
            order_by=order_by,
            time=time,
            category=category,
            sub_category=sub_category,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return serialize_search_page(search_page, page)


def ensure_thumbnail(album_id: str, client) -> Path:
    THUMB_ROOT.mkdir(parents=True, exist_ok=True)
    thumbnail_path = THUMB_ROOT / f"{album_id}{FINAL_SUFFIX}"
    if thumbnail_path.exists():
        if CACHE_ENABLED:
            return thumbnail_path
        thumbnail_path.unlink(missing_ok=True)

    tmp_path = thumbnail_path.with_suffix(".dl")
    client.download_album_cover(album_id, str(tmp_path))
    convert_to_format(tmp_path, thumbnail_path)
    return thumbnail_path


def get_album_cover_path(album_id: str) -> Path:
    """获取漫画封面的本地路径"""
    sanitized_album = str(album_id)
    return PHOTO_ROOT / sanitized_album / f"cover{FINAL_SUFFIX}"


def ensure_album_cover(album_id: str, client) -> Path:
    """确保漫画封面已下载到本地，如果不存在则下载"""
    cover_path = get_album_cover_path(album_id)
    cover_path.parent.mkdir(parents=True, exist_ok=True)
    
    if cover_path.exists():
        if CACHE_ENABLED:
            return cover_path
        cover_path.unlink(missing_ok=True)

    # 下载封面到临时文件，然后转换为目标格式
    # 使用 .webp 作为临时扩展名（Pillow 可以识别）
    # 如果目标格式也是 .webp，则直接下载到目标路径
    if FINAL_SUFFIX.lower() == ".webp":
        # 直接下载到目标路径
        client.download_album_cover(album_id, str(cover_path))
        return cover_path
    else:
        # 下载到临时 .webp 文件，然后转换为目标格式
        tmp_path = cover_path.parent / "cover.webp"
        client.download_album_cover(album_id, str(tmp_path))
        convert_to_format(tmp_path, cover_path)
        return cover_path


def ensure_photo_images(photo: JmPhotoDetail, client) -> List[Path]:
    total = len(photo.page_arr or [])
    photo_dir = get_photo_dir(photo.album_id, photo.photo_id)
    photo_dir.mkdir(parents=True, exist_ok=True)

    glob_pattern = f"*{FINAL_SUFFIX}"
    existing = sorted(photo_dir.glob(glob_pattern))

    if not CACHE_ENABLED:
        for path in existing:
            path.unlink(missing_ok=True)
        existing = []

    if CACHE_ENABLED and total and len(existing) >= total:
        return existing

    indices = list(range(total))
    if CACHE_ENABLED:
        indices = [
            idx for idx in indices
            if not (photo_dir / f"{idx + 1:05}{FINAL_SUFFIX}").exists()
        ]

    if indices:
        def download_one(idx: int):
            target = photo_dir / f"{idx + 1:05}{FINAL_SUFFIX}"
            image = photo.create_image_detail(idx)
            original_suffix = Path(image.filename).suffix or FINAL_SUFFIX
            tmp_path = target.with_suffix(original_suffix)
            client.download_image(
                image.download_url,
                str(tmp_path),
                int(image.scramble_id) if image.scramble_id is not None else None,
                decode_image=DECODE_IMAGES,
            )
            convert_to_format(tmp_path, target)
            logger.info(
                "photo.image.saved album=%s photo=%s index=%d path=%s",
                photo.album_id,
                photo.photo_id,
                idx + 1,
                target,
            )

        with ThreadPoolExecutor(max_workers=IMAGE_THREAD_COUNT) as executor:
            list(executor.map(download_one, indices))

    return sorted(photo_dir.glob(glob_pattern))


def clear_photo_images(photo: JmPhotoDetail) -> List[str]:
    photo_dir = get_photo_dir(photo.album_id, photo.photo_id)
    if not photo_dir.exists():
        return []

    deleted = []
    for file_path in photo_dir.glob(f"*{FINAL_SUFFIX}"):
        file_path.unlink(missing_ok=True)
        deleted.append(file_path.name)
    return deleted


def get_photo_dir(album_id: str, photo_id: str) -> Path:
    sanitized_album = str(album_id)
    sanitized_photo = str(photo_id)
    return PHOTO_ROOT / sanitized_album / sanitized_photo


def convert_to_format(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(source_path) as img:
            if source_path.suffix.lower() == target_path.suffix.lower() and source_path == target_path:
                return
            img.convert("RGB").save(target_path, format="JPEG", quality=90)
    finally:
        if source_path.exists() and source_path != target_path:
            source_path.unlink(missing_ok=True)


def to_public_url(path: Path) -> str:
    relative = path.relative_to(DOWNLOAD_ROOT)
    return f"/downloads/{relative.as_posix()}"


def to_relative_path(path: Path) -> str:
    """返回相对于 DOWNLOAD_ROOT 的路径，去掉 /app/data 前缀"""
    relative = path.relative_to(DOWNLOAD_ROOT)
    return relative.as_posix()


app = get_app()


if __name__ == "__main__":
    import uvicorn

    server_cfg = CONFIG.get("server", {})
    uvicorn.run(
        "api_service.main:app",
        host=server_cfg.get("host", "0.0.0.0"),
        port=int(server_cfg.get("port", 8000)),
        reload=os.getenv("JM_API_RELOAD", "false").lower() == "true",
    )

