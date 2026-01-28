import codecs
import hashlib
import os
import re
import shutil
from functools import cache

import requests

from logs import logger
from ..config import Settings


@cache
def get_settings():
    return Settings()


@cache
def get_apilevel_namespace_map():
    return get_settings().api_namespace


@cache
def get_namespace_apilevel_map():
    return dict([(v, k) for (k, v) in get_settings().api_namespace.items()])


@cache
def get_tos_content():
    tos_path = os.path.join(get_settings().root_path, "ToS")
    with codecs.open(tos_path, "r", "utf8") as f:
        tos_content = f.read()
    return tos_content


@cache
def get_tos_hash():
    tos_content = get_tos_content()
    tos_hash = hashlib.sha256(tos_content.encode()).hexdigest()
    return tos_hash


def cache_file(file_path: str):
    settings = get_settings()
    file_cache_dir = os.path.join(settings.root_path, settings.file_cache_dir)
    if not os.path.exists(file_cache_dir):
        os.makedirs(file_cache_dir, exist_ok=True)
    try:
        with open(file_path, "rb") as f:
            bs = f.read()
    except FileNotFoundError:
        logger.error("File not found: " + file_path)
        return None
    sha256_hash = hashlib.sha256(bs).hexdigest()
    s = re.search(r'(?P<name>[^/\\&\?]+)\.(?P<ext>\w+)', file_path)
    hashed_name = f"{s.group('name')}.{sha256_hash}.{s.group('ext')}"
    hashed_path = os.path.join(file_cache_dir, hashed_name)
    logger.info(f"Caching {file_path} -> {hashed_path}")
    shutil.copy(file_path, hashed_path)
    return hashed_name, hashed_path


def download_file(url, dst="", force: bool = False):
    settings = get_settings()
    file_cache_dir = os.path.join(settings.root_path, settings.file_cache_dir)
    if not dst:
        dst = file_cache_dir
    if not os.path.exists(dst):
        os.makedirs(dst, exist_ok=True)
    local_filename = url.split('/')[-1]
    filepath = os.path.join(dst, local_filename)
    if os.path.exists(filepath) and not force:
        logger.info(f"File {filepath} exists, skipping download")
        return filepath
    logger.info(f"Downloading {url} -> {filepath}")
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        with open(filepath, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    return filepath
