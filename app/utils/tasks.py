import codecs
import concurrent.futures
import hashlib
import json
import os
import re
from collections import defaultdict
from itertools import product
from typing import Union, Tuple
from datetime import datetime

import commentjson
from github import Github
from termcolor import colored

from logs import logger
from .cdn.cloudflare import CloudFlareCDN
from .cdn.ctcdn import CTCDN
from .cdn.ottercloudcdn import OtterCloudCDN
from .common import get_settings, cache_file, download_file
from .git import update_git_repo, get_repo_dir, get_user_repo_name
from .redis import Redis


def regen(task_list: list[str]):
    settings = get_settings()

    logger.info(f"Started regeneration tasks: {task_list}.")
    with concurrent.futures.ThreadPoolExecutor() as executor:
        results = executor.map(regen_task, task_list)
        results_str = ""
        for (task, result) in zip(task_list, results):
            ok = colored("ok", "green") if result else colored("failed", "red")
            results_str += f"{task}: {ok}\n"
        logger.info(f"Regeneration tasks finished with results: {results_str.strip()}")

    cdn_client_list = []
    for cdn in settings.cdn_list:
        if cdn == 'cloudflare':
            cdn_client_list.append(CloudFlareCDN())
        elif cdn == 'ctcdn':
            cdn_client_list.append(CTCDN())
        elif cdn == 'ottercloudcdn':
            cdn_client_list.append(OtterCloudCDN())
    task_cdn_list = list(product(task_list, cdn_client_list))

    logger.info(f"Started CDN refresh tasks: {task_cdn_list}.")
    with concurrent.futures.ThreadPoolExecutor() as executor:
        results = executor.map(refresh_cdn_task,
                               task_cdn_list)  # an iterator of for i in task_cdn_list -> refresh_cdn_task(i)
        results_list = []
        for (task_cdn, result) in zip(task_cdn_list, results):
            task, cdn = task_cdn
            ok = colored("ok", "green") if result else colored("failed", "red")
            results_list.append(f"{task}-{cdn}: {ok}")
        for i in results_list:
            logger.info(f"CDN refresh tasks finished with results: {i.strip()}")


def regen_task(task: str):
    logger.info(f"Started regeneration task: {task}.")
    try:
        redis_client = Redis.create_client()
        task_map = {
            'dalamud': regen_dalamud,
            'dalamud_changelog': regen_dalamud_changelog,
            'plugin': regen_pluginmaster,
            'asset': regen_asset,
            'xl': regen_xivlauncher,
            'xivl': regen_xivlauncher,
            'xivlauncher': regen_xivlauncher,
            'updater': regen_updater,
            'xlassets': regen_xlassets,
        }
        if task in task_map:
            func = task_map[task]
            func(redis_client)
        else:
            raise RuntimeError("Invalid task")
        logger.info(f"Regeneration task {task} finished.")
        return True
    except Exception as e:
        logger.error(e)
        logger.error(f"Regeneration task {task} failed.")
        return False


def refresh_cdn_task(task_cdn: Tuple[str, Union[CloudFlareCDN, CTCDN, OtterCloudCDN]]):
    task, cdn = task_cdn
    logger.info(f"Started CDN refresh task: {cdn}-{task}.")
    try:
        settings = get_settings()
        path_map = {
            'dalamud': ['/Dalamud/Release/VersionInfo', '/Dalamud/Release/Meta'] + \
                       [f'/Release/VersionInfo?track={x}' for x in ['release', 'staging', 'stg', 'canary']],
            'dalamud_changelog': ['/Plugin/CoreChangelog'],
            'plugin': ['/Plugin/PluginMaster', f'/Plugin/PluginMaster?apiLevel={settings.plugin_api_level}',
                       f'/Plugin/PluginMaster?apiLevel={settings.plugin_api_level_test}'],
            'asset': ['/Dalamud/Asset/Meta'],
            'xl': ['/Proxy/Meta', '/Launcher/GetLease'],
            'xivl': ['/Proxy/Meta', '/Launcher/GetLease'],
            'xivlauncher': ['/Proxy/Meta', '/Launcher/GetLease',
                            'https://s3.ffxiv.wang/xivlauncher-cn/releases.win.json', 'https://s3.ffxiv.wang/xivlauncher-cn/releases.beta.json',
                            'https://s3.ffxiv.wang/xivlauncher-cn/XIVLauncherCN-win-Setup.exe', 'https://s3.ffxiv.wang/xivlauncher-cn/XIVLauncherCN-beta-Setup.exe',
                            'https://s3.ffxiv.wang/xivlauncher-cn/XIVLauncherCN-win-Portable.7z', 'https://s3.ffxiv.wang/xivlauncher-cn/XIVLauncherCN-beta-Portable.7z'],
            'updater': ['/Updater/Release/VersionInfo', '/Updater/ChangeLog'],
            'xlassets': ['/XLAssets/integrity', 'https://s3.ffxiv.wang/xlassets/patchinfo/latest.json'],
        }
        if task in path_map:
            cdn.purge(path_map[task])
        else:
            raise RuntimeError("Invalid task")
        logger.info(f"CDN refresh task {cdn}-{task} finished.")
        return True
    except Exception as e:
        logger.error(e)
        logger.error(f"CDN refresh task {cdn}-{task} failed.")
        return False


DEFAULT_META = {
    "Changelog": "",
    "Tags": [],
    "IsHide": False,
    "TestingAssemblyVersion": None,
    "AcceptsFeedback": True,
    "FeedbackMessage": None,
    "FeedbackWebhook": None,
}


def parsing_pluginmaster(redis_client, settings, repo_url, plugin_list=None) -> tuple[list[dict], list[str], str]:
    if plugin_list is None:
        plugin_list = list()
    plugin_list_length = len(plugin_list)
    (_, repo_name) = get_user_repo_name(repo_url)
    (_, repo) = update_git_repo(repo_url)
    branch = repo.active_branch.name
    plugin_namespace = f"plugin-{repo_name}-{branch}"
    logger.info(f"plugin_namespace: {plugin_namespace}")
    plugin_repo_dir = get_repo_dir(repo_url)
    pluginmaster = []
    plugin_name_list = []
    channel_map = {
        'stable': 'stable',
        'testing': 'testing-live'
    }
    jsonc = commentjson
    stable_dir = os.path.join(plugin_repo_dir, channel_map['stable'])
    testing_dir = os.path.join(plugin_repo_dir, channel_map['testing'])
    if not os.path.exists(testing_dir):
        os.mkdir(testing_dir)

    # Load last update time
    last_updated = {}
    state_path = os.path.join(plugin_repo_dir, 'state.json')
    with codecs.open(state_path, 'r', 'utf8') as f:
        state = json.load(f)
    for (channel, channel_meta) in state['Channels'].items():
        for (plugin, plugin_meta) in channel_meta['Plugins'].items():
            last_updated[plugin] = int(datetime.fromisoformat(re.sub(r'(\.\d{6})\d+(?=[+-]\d{2}:\d{2}$)', r'\1', plugin_meta['TimeBuilt'])).timestamp())
    # Generate pluginmaster
    for plugin_dir in [stable_dir, testing_dir]:
        for plugin in os.listdir(plugin_dir):
            try:
                with codecs.open(os.path.join(plugin_dir, f'{plugin}/{plugin}.json'), 'r', 'utf8') as f:
                    plugin_meta = jsonc.load(f)
            except FileNotFoundError:
                logger.error(f"Cannot find plugin meta file for {plugin}")
                continue
            except Exception as e:
                try:
                    with codecs.open(os.path.join(plugin_dir, f'{plugin}/{plugin}.json'), 'r', 'utf-8-sig') as f:
                        plugin_meta = jsonc.load(f)
                except Exception as e:
                    logger.error(f"Cannot parse plugin meta file for {plugin}")
                    continue
            api_level = int(plugin_meta.get("DalamudApiLevel", 0))
            if settings.plugin_api_level - api_level >= 1:
                continue
            if plugin_list_length > 0 and plugin in plugin_list:
                continue
            for key, value in DEFAULT_META.items():
                if key not in plugin_meta:
                    plugin_meta[key] = value
            is_testing = plugin_dir == testing_dir
            plugin_meta["IsTestingExclusive"] = is_testing
            if is_testing:
                plugin_meta["TestingAssemblyVersion"] = plugin_meta["AssemblyVersion"]
            download_count = redis_client.hget(f'{settings.redis_prefix}plugin-count', plugin) or 0
            plugin_meta["DownloadCount"] = int(download_count)
            plugin_meta["LastUpdate"] = last_updated.get(plugin, plugin_meta.get("LastUpdate", 0))
            plugin_meta["DownloadLinkInstall"] = settings.hosted_url.rstrip('/') \
                                                 + '/Plugin/Download/' + f"{plugin}?isUpdate=False&isTesting=False&branch=api{api_level}"
            plugin_meta["DownloadLinkUpdate"] = settings.hosted_url.rstrip('/') \
                                                + '/Plugin/Download/' + f"{plugin}?isUpdate=True&isTesting=False&branch=api{api_level}"
            plugin_meta["DownloadLinkTesting"] = settings.hosted_url.rstrip('/') \
                                                 + '/Plugin/Download/' + f"{plugin}?isUpdate=False&isTesting=True&branch=api{api_level}"
            plugin_latest_path = os.path.join(plugin_dir, f'{plugin}/latest.zip')
            plugin_meta["IconUrl"] = f"https://s3test.ffxiv.wang/plugindistd17/stable/{plugin}/images/icon.png"
            (hashed_name, _) = cache_file(plugin_latest_path)
            plugin_name = f"{plugin}-testing" if is_testing else plugin
            redis_client.hset(f'{settings.redis_prefix}{plugin_namespace}', plugin_name, hashed_name)
            pluginmaster.append(plugin_meta)
            plugin_name_list.append(plugin)

    return pluginmaster, plugin_name_list, plugin_namespace


def regen_pluginmaster(redis_client=None, repo_url: str = ''):
    logger.info("Start regenerating pluginmaster.")
    settings = get_settings()
    if not redis_client:
        redis_client = Redis.create_client()
    if not repo_url:
        repo_url = settings.plugin_repo

    repo_url_goatcorp = settings.plugin_repo_goatcorp

    pluginmaster_cn, plugin_name_list_cn, plugin_namespace = parsing_pluginmaster(redis_client, settings, repo_url)
    pluginmaster, _, _ = parsing_pluginmaster(redis_client, settings, repo_url_goatcorp, plugin_name_list_cn)
    pluginmaster += pluginmaster_cn

    redis_client.hset(f'{settings.redis_prefix}{plugin_namespace}', 'pluginmaster', json.dumps(pluginmaster))
    plugin_name_list = []
    for plugin in pluginmaster:
        plugin_name = plugin['InternalName']
        plugin_name_list.append(plugin_name)
    redis_client.delete(f'{settings.redis_prefix}plugin_name_list')
    redis_client.rpush(f'{settings.redis_prefix}plugin_name_list', *plugin_name_list)
    # print(f"Regenerated Pluginmaster for {plugin_namespace}: \n" + str(json.dumps(pluginmaster, indent=2)))


def regen_asset(redis_client=None):
    logger.info("Start regenerating dalamud assets.")
    if not redis_client:
        redis_client = Redis.create_client()
    settings = get_settings()
    update_git_repo(settings.asset_repo)
    asset_repo_dir = get_repo_dir(settings.asset_repo)
    with codecs.open(os.path.join(asset_repo_dir, "asset.json"), "r") as f:
        asset_json = json.load(f)
    asset_list = []
    cheatplugin_hash = ""
    for asset in asset_json["Assets"]:
        file_path = os.path.join(asset_repo_dir, asset["FileName"])
        (hashed_name, _) = cache_file(file_path)
        if "github" in asset["Url"]:  # only replace the github urls
            asset["Url"] = settings.hosted_url.rstrip('/') + '/File/Get/' + hashed_name
        if "cheatplugin.json" in asset["FileName"]:
            cheatplugin_hash = asset["Hash"]
        asset_list.append(asset)
    asset_json["Assets"] = asset_list
    # print("Regenerated Assets: \n" + str(json.dumps(asset_json, indent=2)))
    redis_client.hset(f'{settings.redis_prefix}asset', 'meta', json.dumps(asset_json))
    if cheatplugin_hash:
        redis_client.hset(f'{settings.redis_prefix}asset', 'cheatplugin_hash', cheatplugin_hash)
        with open(os.path.join(asset_repo_dir, "UIRes/cheatplugin.json"), "rb") as f:
            bs = f.read()
            cheatplugin_hash_sha256 = hashlib.sha256(bs).hexdigest().upper()
            redis_client.hset(f'{settings.redis_prefix}asset', 'cheatplugin_hash_sha256', cheatplugin_hash_sha256)


def regen_dalamud(redis_client=None):
    logger.info("Start regenerating dalamud distribution.")
    if not redis_client:
        redis_client = Redis.create_client()
    settings = get_settings()
    (__, repo) = update_git_repo(settings.distrib_repo)
    branch_prefix = ''
    branch_name = repo.active_branch.name
    if branch_name not in ('main', 'master'):
        branch_prefix = f'{branch_name}-'
    distrib_repo_dir = get_repo_dir(settings.distrib_repo)
    runtime_verlist = []
    # release_version = {}
    for track in ["release", "stg", "canary"]:
        dist_dir = distrib_repo_dir if track == "release" else \
            os.path.join(distrib_repo_dir, track)
        with codecs.open(os.path.join(dist_dir, 'version'), 'r', 'utf8') as f:
            version_json = json.load(f)
        if version_json['RuntimeRequired'] and version_json['RuntimeVersion'] not in runtime_verlist:
            runtime_verlist.append(version_json['RuntimeVersion'])
        ext_format = settings.dalamud_format  # zip or 7z
        dalamud_path = os.path.join(dist_dir, f"latest.{ext_format}")
        (hashed_name, _) = cache_file(dalamud_path)
        version_json['downloadUrl'] = settings.hosted_url.rstrip('/') + f'/File/Get/{hashed_name}'
        version_json['track'] = track
        if track == 'release':
            version_json['changelog'] = []
        if 'key' not in version_json and 'Key' not in version_json:
            version_json['key'] = None
        redis_client.hset(f'{settings.redis_prefix}dalamud', f'dist-{branch_prefix}{track}', json.dumps(version_json))
        # if track == 'release':
        #     release_version = version_json
    for version in runtime_verlist:
        desktop_url = f'https://dotnetcli.azureedge.net/dotnet/WindowsDesktop/{version}/windowsdesktop-runtime-{version}-win-x64.zip'
        (hashed_name, _) = cache_file(download_file(desktop_url))
        redis_client.hset(f'{settings.redis_prefix}runtime', f'desktop-{version}', hashed_name)
        dotnet_url = f'https://dotnetcli.azureedge.net/dotnet/Runtime/{version}/dotnet-runtime-{version}-win-x64.zip'
        (hashed_name, _) = cache_file(download_file(dotnet_url))
        redis_client.hset(f'{settings.redis_prefix}runtime', f'dotnet-{version}', hashed_name)
    for hash_file in os.listdir(os.path.join(distrib_repo_dir, 'runtimehashes')):
        version = re.search(r'(?P<ver>.*)\.json$', hash_file).group('ver')
        (hashed_name, _) = cache_file(os.path.join(distrib_repo_dir, f'runtimehashes/{hash_file}'))
        redis_client.hset(f'{settings.redis_prefix}runtime', f'hashes-{version}', hashed_name)
    # return release_version


def regen_dalamud_changelog(redis_client=None):
    logger.info("Start regenerating dalamud changelog.")
    if not redis_client:
        redis_client = Redis.create_client()
    settings = get_settings()
    dalamud_repo_url = settings.dalamud_repo
    user, repo_name = get_user_repo_name(dalamud_repo_url)
    gh = Github(None if not settings.github_token else settings.github_token)
    repo = gh.get_repo(f'{user}/{repo_name}')
    tags = repo.get_tags()
    sliced_tags = list(tags[:11])  # only care about latest 10 tags
    changelogs = []
    skip_prefix = ['build:', 'Merge pull request', 'Merge branch']
    for (idx, tag) in enumerate(sliced_tags[:-1]):
        next_tag = sliced_tags[idx + 1]
        changes = []
        diff = repo.compare(next_tag.commit.sha, tag.commit.sha)
        for commit in diff.commits:
            msg = commit.commit.message
            if any([msg.startswith(x) for x in skip_prefix]):
                continue
            changes.append({
                'author': commit.commit.author.name,
                'message': msg.split('\n')[0],
                'sha': commit.sha,
                'date': commit.commit.author.date.isoformat()
            })
        changelogs.append({
            'version': tag.name,
            'date': tag.commit.commit.author.date.isoformat(),
            'changes': changes,
        })
    redis_client.hset(f'{settings.redis_prefix}dalamud', 'changelog', json.dumps(changelogs))


def regen_xivlauncher(redis_client=None):
    logger.info("Start regenerating xivlauncher distribution.")
    if not redis_client:
        redis_client = Redis.create_client()
    settings = get_settings()
    xivl_repo_url = settings.xivl_repo
    s = re.search(r'github.com[\/:](?P<user>.+)\/(?P<repo>.+)\.git', xivl_repo_url)
    user, repo_name = s.group('user'), s.group('repo')
    gh = Github(None if not settings.github_token else settings.github_token)
    repo = gh.get_repo(f'{user}/{repo_name}')
    releases = repo.get_releases()
    pre_release = None
    release = None
    latest_release = releases[0]
    if latest_release.prerelease:
        pre_release = latest_release
        for r in releases:
            if not r.prerelease:
                release = r
                break
    else:
        pre_release = release = latest_release

    for (idx, rel) in enumerate([pre_release, release]):
        release_type = 'prerelease' if idx == 0 else 'release'
        redis_client.hset(f'{settings.redis_prefix}xivlauncher', f'{release_type}-tag', rel.tag_name)
        changelog = ''
        for asset in rel.get_assets():
            asset_filepath = download_file(asset.browser_download_url, force=True)  # overwrite file
            if asset.name == 'RELEASES':
                with codecs.open(asset_filepath, 'r', 'utf8') as f:
                    releases_list = f.read()
                redis_client.hset(f'{settings.redis_prefix}xivlauncher', f'{release_type}-releaseslist', releases_list)
                continue
            if asset.name == 'CHANGELOG.txt':
                with codecs.open(asset_filepath, 'r', 'utf8') as f:
                    changelog = f.read()
            (hashed_name, _) = cache_file(asset_filepath)
            redis_client.hset(
                f'{settings.redis_prefix}xivlauncher',
                f'{release_type}-{asset.name}',
                hashed_name
            )
        track = release_type.capitalize()
        meta = {
            'releasesInfo': f"/Proxy/Update/{track}/RELEASES",
            'version': rel.tag_name,
            'url': rel.html_url,
            'changelog': changelog,
            'when': rel.published_at.isoformat(),
        }
        redis_client.hset(
            f'{settings.redis_prefix}xivlauncher',
            f'{release_type}-meta',
            json.dumps(meta)
        )


def regen_updater(redis_client=None):
    logger.info("Start regenerating Updater distribution.")
    if not redis_client:
        redis_client = Redis.create_client()
    settings = get_settings()
    redis_client.delete(f'{settings.redis_prefix}updater')
    updater_repo_url = settings.updater_repo
    s = re.search(r'github.com[\/:](?P<user>.+)\/(?P<repo>.+)\.git', updater_repo_url)
    user, repo_name = s.group('user'), s.group('repo')
    gh = Github(None if not settings.github_token else settings.github_token)
    repo = gh.get_repo(f'{user}/{repo_name}')
    releases = repo.get_releases()
    last_release = next((r for r in releases if not r.prerelease), None)
    pre_release = next((r for r in releases if r.prerelease), None)
    if last_release is None or pre_release is None:
        last_release = last_release or pre_release
        pre_release = pre_release or last_release
    for release in (last_release, pre_release):
        release_type = 'prerelease' if release.prerelease else 'release'
        assets = release.get_assets()
        for asset in assets:
            file_name = asset.name
            if file_name == 'release.zip':
                asset_filepath = download_file(asset.browser_download_url, force=True)  # overwrite file
                (hashed_name, _) = cache_file(asset_filepath)
                redis_client.hset(
                    f'{settings.redis_prefix}updater',
                    f'{release_type}-asset',
                    hashed_name
                )
    version_dict = {
        'release': last_release.tag_name,
        'prerelease': pre_release.tag_name,
    }
    redis_client.hset(
        f'{settings.redis_prefix}updater',
        f'version',
        json.dumps(version_dict)
    )


def regen_xlassets(redis_client=None):
    logger.info("Start regenerating XLAssets distribution")
    if not redis_client:
        redis_client = Redis.create_client()
    settings = get_settings()
    xlassets_repo = settings.xlassets_repo
    update_git_repo(xlassets_repo)
    integrity_path = os.path.join(get_repo_dir(xlassets_repo), 'integrity')
    integrity_files = os.listdir(integrity_path)
    integrity_files.sort(reverse=True)
    latest_integrity = integrity_files[0]
    with codecs.open(os.path.join(integrity_path, latest_integrity), 'r', 'utf8') as f:
        integrity_json = json.load(f)
    redis_client.hset(
        f'{settings.redis_prefix}xlassets',
        f'version',
        latest_integrity.split('.json')[0]
    )
    redis_client.hset(
        f'{settings.redis_prefix}xlassets',
        f'json',
        json.dumps(integrity_json)
    )
