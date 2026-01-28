# -*- coding: utf-8 -*-
# cython:language_level=3
import asyncio
import json
from datetime import datetime, timezone, timedelta
from io import BytesIO

from fastapi import APIRouter, HTTPException, Depends, Request, Form, UploadFile
from fastapi.responses import RedirectResponse, PlainTextResponse, HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates

from app.config import Settings
from app.utils.cdn.ottercloudcdn import OtterCloudCDN
from app.utils.common import get_settings, flush_stg_code
from app.utils.dalamud_log_analysis import analysis
from app.utils.front import flash
from app.utils.redis import RedisFeedBack
from app.utils.tasks import regen

router = APIRouter()
template = Jinja2Templates("templates")


# region admin index page
@router.get('/', response_class=HTMLResponse)
async def front_admin_index(request: Request):
    return template.TemplateResponse("admin_index.html", {"request": request})


async def run_command(command):
    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await process.communicate()

    if process.returncode == 0:
        return stdout.decode().strip()
    else:
        raise RuntimeError(f"Command failed with return code {process.returncode}: {stderr.decode().strip()}")


@router.get('/download_logs', response_class=PlainTextResponse)
async def front_admin_download_logs():
    result = await run_command("journalctl -xeu XLWeb-fastapi")
    log_path = r'./logs/XLWebServices.log'
    with open(log_path, 'w') as f:
        f.write(result)
    return FileResponse(log_path)


@router.get('/stop_svr')
async def front_admin_stop():
    await run_command("systemctl stop XLWeb-fastapi")
    return


@router.get('/restart_svr')
async def front_admin_restart():
    await run_command("systemctl restart XLWeb-fastapi")
    return


@router.get('/update_svr')
async def front_admin_update():
    await run_command("update_XLWeb")
    return


@router.get('/stg_code')
async def front_admin_stg_code(request: Request):
    flash(request, 'info', f'Stg Code为{Settings.stg_code}')
    return


# endregion

# region feedback
@router.get('/feedback', response_class=HTMLResponse)
async def front_admin_feedback_get(request: Request):
    r_fb = RedisFeedBack.create_client()
    feedback_list = r_fb.keys('feedback|*')
    return_list = []
    for i in feedback_list:
        temp_list = i.replace('feedback|', '').split('|')
        return_list.append(temp_list)
    return template.TemplateResponse("feedback_admin.html", {"request": request, "feedback_list": return_list})


@router.get('/feedback/export', response_class=HTMLResponse)
async def front_admin_feedback_export_get(request: Request):
    r_fb = RedisFeedBack.create_client()
    feedback_list = r_fb.keys('feedback|*')
    return_dict = {}
    for i in feedback_list:
        dhash, plugin_name, order_id = i.replace('feedback|', '').split('|')
        if plugin_name not in return_dict:
            return_dict[plugin_name] = []
        feedback = r_fb.hgetall(f'feedback|{dhash}|{plugin_name}|{order_id}')
        create_time = datetime.fromtimestamp(float(feedback.get('create_time', 0)), tz=timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
        return_dict[plugin_name].append({
            "order_id": order_id,
            "dhash": dhash,
            "version": feedback['version'],
            "content": feedback['content'],
            "exception": feedback['exception'],
            "reporter": feedback['reporter'],
            "create_time": create_time,
        })
    for k, v in return_dict.items():
        return_dict[k] = sorted(v, key=lambda x: x['order_id'], reverse=True)
    return template.TemplateResponse("feedback_export.html", {"request": request, "export_dict": return_dict})


@router.get('/feedback/detail/{plugin_name}/{feedback_id}', response_class=HTMLResponse)
async def front_admin_feedback_detail_get(request: Request, plugin_name: str, feedback_id: int, dhash: str | None = None):
    r_fb = RedisFeedBack.create_client()
    feedback = r_fb.hgetall(f'feedback|{dhash}|{plugin_name}|{feedback_id}')
    if not feedback:
        raise HTTPException(status_code=404, detail="Feedback not found")
    feedback['reply_log'] = json.loads(feedback['reply_log'])
    feedback['create_time'] = datetime.fromtimestamp(float(feedback.get('create_time', 0)), tz=timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    return template.TemplateResponse("feedback_admin_detail.html", {"request": request, "detail": feedback, "plugin_name": plugin_name, "feedback_id": feedback_id})


@router.get('/feedback/solve/{feedback_id}', response_class=RedirectResponse)
async def front_admin_feedback_solve_get(request: Request, feedback_id: int, referer: str | None = None):
    r_fb = RedisFeedBack.create_client()
    feedback_list = r_fb.keys(f'feedback|*|{feedback_id}')
    if len(feedback_list) == 1:
        r_fb.delete(feedback_list[0])
        if referer == "export":
            return RedirectResponse(request.app.url_path_for('front_admin_feedback_export_get'))
        else:
            return RedirectResponse(request.app.url_path_for('front_admin_feedback_get'))
    elif len(feedback_list) > 1:
        raise HTTPException(status_code=400, detail="More than one feedback found.")
    else:
        raise HTTPException(status_code=400, detail="No feedback found.")


@router.post('/feedback/reply/{feedback_id}', response_class=RedirectResponse)
async def front_admin_feedback_reply_post(request: Request, feedback_id: int, content: str):
    raise HTTPException(status_code=404, detail="Not implemented yet.")


# endregion

# region flush
@router.get('/flush', response_class=HTMLResponse)
async def front_admin_flush_get(request: Request):
    return template.TemplateResponse("flush.html", {"request": request})


@router.post('/flush')
async def front_admin_flush_post(request: Request, action: str = Form(...), task_type: int = Form(...), content: str = Form(...), ottercloudcdn: OtterCloudCDN = Depends(OtterCloudCDN)):
    try:
        url_list = content.replace('\r', '').split('\n')
        if action == 'prefetch':
            ottercloudcdn.prefetch(task_type, url_list)
            flash(request, 'success', f'预取任务已提交')
        if action == 'flushUrl':
            ottercloudcdn.refresh(task_type, url_list)
            flash(request, 'success', f'刷新任务已完成')
    except Exception as e:
        flash(request, 'error', f'任务失败，{e}', )
    finally:
        return RedirectResponse(url=request.app.url_path_for('front_admin_flush_get'), status_code=303)


@router.get('/flush_cache')
async def front_admin_flush_cache_get(request: Request, task: str | None = None):
    if task:
        match task:
            case 'dalamud':
                regen(['dalamud', 'dalamud_changelog'])
            case 'asset':
                regen(['asset'])
            case 'plugin':
                regen(['plugin'])
            case 'xivlauncher':
                regen(['xivlauncher'])
            case 'updater':
                regen(['updater'])
            case 'xlassets':
                regen(['xlassets'])
            case 'all':
                regen(['dalamud', 'dalamud_changelog', 'asset', 'plugin', 'xivlauncher', 'updater', 'xlassets'])
            case _:
                flash(request, 'error', '任务不存在', )
                return RedirectResponse(url=request.app.url_path_for("front_admin_flush_get"))
        flash(request, 'success', f'刷新{task if task != "all" else "全部"}任务已完成')
    else:
        raise HTTPException(status_code=400, detail="No task specified.")
    if request.headers.get('referer') and 'flush' in request.headers.get('referer'):
        return RedirectResponse(url=request.app.url_path_for("front_admin_flush_get"), status_code=303)
    else:
        return RedirectResponse(url=request.app.url_path_for("front_admin_index"), status_code=303)


@router.get('/flush_stg_code')
async def front_admin_flush_stg_code(request: Request):
    stg_code = flush_stg_code()
    flash(request, 'success', f'刷新Stg Code已完成，新的key为{stg_code}')
    return RedirectResponse(url=request.app.url_path_for("front_admin_index"), status_code=303)


# endregion

# region analytics
@router.get('/log_analytics', response_class=HTMLResponse)
async def front_admin_log_analytics_get(request: Request):
    return template.TemplateResponse("log_analysis.html", {"request": request})


@router.post('/log_analytics', )
async def front_admin_log_analytics_post(request: Request, file: UploadFile = Form(...), settings: Settings = Depends(get_settings)):
    file_byte = await file.read()
    file = BytesIO(file_byte)
    analysis_result, log_file_type = analysis(file, settings.plugin_api_level)
    return template.TemplateResponse("log_analysis_result.html", {"request": request, "analysis_result": analysis_result, "log_file_type": log_file_type})

# endregion
