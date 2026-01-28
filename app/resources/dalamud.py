import asyncio
import hashlib
import json

import httpx
import orjson
from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException, Depends, Query, BackgroundTasks
from fastapi.responses import RedirectResponse, PlainTextResponse
from app.utils import httpx_client
from app.config import Settings
from app.utils.common import get_settings, get_tos_content, get_tos_hash
from app.utils.redis import Redis

from app.utils.tasks import regen

router = APIRouter()

api_secret = get_settings().ga_api_secret
measurement_id = "G-W3HJPGVM1J"


class Analytics(BaseModel):
    client_id: str
    aid: str = ""
    user_id: str
    server_id: str
    os: str
    cheat_banned_hash: str = ""
    dalamud_version: str = ""
    is_testing: bool = False
    plugin_count: int
    plugin_list: list = Field(default_factory=lambda: [])
    mid: str = ""


@router.get("/Asset/Meta")
async def dalamud_assets(settings: Settings = Depends(get_settings)):
    r = Redis.create_client()
    asset_str = r.hget(f'{settings.redis_prefix}asset', 'meta')
    if not asset_str:
        raise HTTPException(status_code=404, detail="Asset meta not found")
    asset_json = json.loads(asset_str)
    return asset_json


@router.get("/Release/VersionInfo")
async def dalamud_release(settings: Settings = Depends(get_settings), track: str = "release"):
    if track == "staging":
        track = "stg"
    if not track:
        track = "release"
    r = Redis.create_client()
    version_str = r.hget(f'{settings.redis_prefix}dalamud', f'dist-{track}')
    if not version_str:
        raise HTTPException(status_code=400, detail="Invalid track")
    version_json = json.loads(version_str)
    return version_json


@router.get("/Release/Meta")
async def dalamud_release_meta(settings: Settings = Depends(get_settings)):
    meta_json = {}
    r = Redis.create_client()
    for track in ['release', 'stg', 'canary']:
        version_str = r.hget(f'{settings.redis_prefix}dalamud', f'dist-{track}')
        if not version_str:
            continue
        version_json = json.loads(version_str)
        meta_json[track] = version_json
    return meta_json


@router.get("/Release/Runtime/{kind_version:path}")
async def dalamud_runtime(kind_version: str, settings: Settings = Depends(get_settings)):
    if len(kind_version.split('/')) != 2:
        return HTTPException(status_code=400, detail="Invalid path")
    kind, version = kind_version.split('/')
    r = Redis.create_client()
    kind_map = {
        'WindowsDesktop': 'desktop',
        'DotNet': 'dotnet',
        'Hashes': 'hashes'
    }
    if kind not in kind_map:
        raise HTTPException(status_code=400, detail="Invalid kind")
    hashed_name = r.hget(f'{settings.redis_prefix}runtime', f'{kind_map[kind]}-{version}')
    if not hashed_name:
        raise HTTPException(status_code=400, detail="Invalid version")
    return RedirectResponse(f"/File/Get/{hashed_name}", status_code=302)


@router.post("/Release/ClearCache")
async def release_clear_cache(background_tasks: BackgroundTasks, key: str = Query(), settings: Settings = Depends(get_settings)):
    if key != settings.cache_clear_key:
        raise HTTPException(status_code=400, detail="Cache clear key not match")
    background_tasks.add_task(regen, ['dalamud', 'dalamud_changelog'])
    return {'message': 'Background task was started.'}


@router.post("/Asset/ClearCache")
async def asset_clear_cache(background_tasks: BackgroundTasks, key: str = Query(), settings: Settings = Depends(get_settings)):
    if key != settings.cache_clear_key:
        raise HTTPException(status_code=400, detail="Cache clear key not match")
    background_tasks.add_task(regen, ['asset'])
    return {'message': 'Background task was started.'}


async def _analytics_post(url: str, payload: dict):
    content = orjson.dumps(payload)
    for attempt in range(3):
        try:
            resp = await httpx_client.post(
                url,
                content=content,
                headers={"content-type": "application/json"},
            )
            return resp.status_code
        except (httpx.RequestError, httpx.HTTPStatusError):
            if attempt == 2:
                return None
            await asyncio.sleep(0.5 * (attempt + 1))


@router.post("/Analytics/Start")
async def analytics_start(analytics: Analytics, settings: Settings = Depends(get_settings)):
    ga_url = f"https://www.google-analytics.com/mp/collect?measurement_id={measurement_id}&api_secret={api_secret}"
    r = Redis.create_client()
    cheatplugin_hash = r.hget(f'{settings.redis_prefix}asset', 'cheatplugin_hash')
    cheatplugin_hash_sha256 = r.hget(f'{settings.redis_prefix}asset', 'cheatplugin_hash_sha256')
    cheat_banned_hash_valid = analytics.cheat_banned_hash and \
                              (cheatplugin_hash == analytics.cheat_banned_hash or cheatplugin_hash_sha256 == analytics.cheat_banned_hash)
    plugin_name_list = r.lrange(f'{settings.redis_prefix}plugin_name_list', 0, -1)
    plugin_3rd_list = list(set(analytics.plugin_list) - set(plugin_name_list))
    user_id = hashlib.blake2s(analytics.user_id.encode(), digest_size=8).hexdigest()
    user_props_base = {
        "HomeWorld": {"value": analytics.server_id},
        "Cheat_Banned_Hash_Valid": {"value": cheat_banned_hash_valid},
        "Client": {"value": analytics.aid or analytics.client_id},
        "os": {"value": analytics.os},
        "dalamud_version": {"value": analytics.dalamud_version},
        "is_testing": {"value": analytics.is_testing},
        "plugin_count": {"value": analytics.plugin_count},
        "machine_id": {"value": analytics.mid},
    }
    event_params = {
        "server_id": analytics.server_id,
        "engagement_time_msec": "100",
        "session_id": analytics.client_id,
    }
    data_ga = {
        "client_id": analytics.client_id,
        "user_id": user_id,
        "user_properties": user_props_base,
        "events": [{"name": "start_dalamud", "params": event_params}],
    }
    data_oa = {
        "client_id": analytics.client_id,
        "user_id": user_id,
        "user_properties": {
            **user_props_base,
            "plugin_3rd_list": {"value": plugin_3rd_list},
        },
        "events": [{"name": "start_dalamud", "params": event_params}],
    }

    await asyncio.gather(
        _analytics_post(ga_url, data_ga),
        _analytics_post("http://127.0.0.1:7000/collect", data_oa),
    )
    return {'message': 'OK'}


@router.get("/TOS")
async def dalamud_tos(tosHash: bool = False, settings: Settings = Depends(get_settings)):
    if tosHash:
        tos_hash = get_tos_hash()
        return {'message': 'OK', 'tosHash': tos_hash.upper()}
    tos_content = get_tos_content()
    return PlainTextResponse(tos_content)


class StgCode(BaseModel):
    code: str


@router.post("/Check/StgCode")
async def check_stg_code(StgCode: StgCode, settings: Settings = Depends(get_settings)):
    r = Redis.create_client()
    if StgCode.code != r.hget('settings', 'stg_code'):
        raise HTTPException(status_code=400, detail="Invalid code")
    return {'message': 'OK'}
