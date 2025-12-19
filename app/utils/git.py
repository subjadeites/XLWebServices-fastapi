import os
import re
import git
from .common import get_settings


def get_git_hash(repo_path: str = '', short_sha: bool = True, check_dirty: bool = True):
    repo = git.Repo(repo_path)
    sha = repo.head.commit.hexsha
    if short_sha:
        sha = sha[:7]
    dirty = '-dirty' if repo.is_dirty() and check_dirty else ''
    return f'{sha}{dirty}'


def get_user_repo_name(git_url: str):
    s = re.search(r'[\/:](?P<user>[^\/]+)\/(?P<repo>[^\/]+)\.git', git_url)
    user, repo_name = s.group('user'), s.group('repo')
    return user, repo_name


def get_repo_dir(git_url: str):
    settings = get_settings()
    if git_url == settings.plugin_repo:
        repo_name = "PluginDistD17_ottercorp"
    else:
        (_, repo_name) = get_user_repo_name(git_url)
    repo_root_dir = os.path.join(settings.root_path, settings.repo_cache_dir)
    repo_dir = os.path.join(repo_root_dir, repo_name)
    if not os.path.exists(repo_dir) or not os.path.isdir(repo_dir):
        os.makedirs(repo_dir, exist_ok=True)
    return repo_dir


def get_git_repo(git_url: str, shallow: bool = True):
    repo_dir = get_repo_dir(git_url)
    if os.path.exists(os.path.join(repo_dir, ".git")):
        return git.Repo(repo_dir)
    options = ['--depth=1'] if shallow else []
    return git.Repo.clone_from(
        git_url,
        repo_dir,
        multi_options=options
    )


def update_git_repo(git_url: str):
    repo = get_git_repo(git_url)
    pull = repo.remotes.origin.pull(force=True)
    info = pull[0]
    assert info.flags & info.ERROR == 0, f"Error while pulling repo {git_url}"
    assert info.flags & info.REJECTED == 0, f"Rejected while pulling repo {git_url}"
    return info, repo
