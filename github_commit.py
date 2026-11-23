"""Commit admin-written files back to the GitHub repo.

Called after any write that must survive a Render restart — photo uploads,
vacancy posts, gallery edits, new applications — so the change lands in the
repo and ships on the next deploy.

Requires env vars:
  GITHUB_TOKEN    fine-grained PAT with contents:write on this repo only
  GITHUB_REPO     "username/repo-name"
  GITHUB_BRANCH   "main" (or "master")
"""
import base64
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main").strip()

_API = "https://api.github.com"


def _headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_sha(repo_path):
    """Get the current SHA of a file in the repo (needed to update it)."""
    url = f"{_API}/repos/{GITHUB_REPO}/contents/{repo_path}?ref={GITHUB_BRANCH}"
    try:
        req = Request(url, headers=_headers())
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("sha")
    except HTTPError as e:
        if e.code == 404:
            return None
        raise


def commit_file(local_path, repo_path, message, retries=2):
    """Commit one file to GitHub. Returns True on success, False on failure.

    Silently returns False if env vars aren't set (so local dev doesn't crash).
    Logs every failure to stdout for the Render log.
    """
    if not (GITHUB_TOKEN and GITHUB_REPO):
        print(f"[github_commit] skipped ({repo_path}): env vars not set", flush=True)
        return False

    local_path = Path(local_path)
    if not local_path.exists():
        print(f"[github_commit] skipped ({repo_path}): local file missing", flush=True)
        return False

    for attempt in range(1, retries + 1):
        try:
            sha = _get_sha(repo_path)
            payload = {
                "message": message,
                "content": base64.b64encode(local_path.read_bytes()).decode("ascii"),
                "branch": GITHUB_BRANCH,
            }
            if sha:
                payload["sha"] = sha
            url = f"{_API}/repos/{GITHUB_REPO}/contents/{repo_path}"
            req = Request(
                url, method="PUT", headers={**_headers(), "Content-Type": "application/json"},
                data=json.dumps(payload).encode(),
            )
            with urlopen(req, timeout=15) as r:
                ok = r.status in (200, 201)
                print(f"[github_commit] {repo_path}: {'committed' if ok else 'failed'}", flush=True)
                return ok
        except HTTPError as e:
            body = e.read().decode()[:200]
            print(f"[github_commit] {repo_path} attempt {attempt} HTTP {e.code}: {body}", flush=True)
            if e.code == 409:  # conflict — retry once with fresh SHA
                time.sleep(1)
                continue
            return False
        except Exception as e:
            print(f"[github_commit] {repo_path} attempt {attempt} error: {e}", flush=True)
            if attempt < retries:
                time.sleep(1)
                continue
            return False
    return False


def commit_data_folder(message):
    """Convenience: commit the DB and everything under data/photos/.

    Called once after a batch of related writes (e.g. after photo upload +
    DB update) so we don't commit twice.
    """
    from app import DB_PATH, DATA
    ok_db = commit_file(DB_PATH, "data/persman.db", message + " (db)")
    ok_photos = True
    photos_dir = DATA / "photos"
    if photos_dir.exists():
        for f in photos_dir.iterdir():
            if f.is_file():
                ok_photos &= commit_file(f, f"data/photos/{f.name}", message + f" ({f.name})")
    return ok_db and ok_photos