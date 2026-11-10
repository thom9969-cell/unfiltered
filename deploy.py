#!/usr/bin/env python3
"""
Unfiltered Advisory LLC — One-command Deployment Script
Pushes local changes to GitHub (thom9969-cell/unfiltered) and deploys
directly to Cloudflare Workers (rapid-fog-4df0 -> unfilteredadvisoryllc.com).
"""

import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(PROJECT_DIR, ".deploy_credentials.json")

ACCOUNT_ID = "b6fc95df0b8f30baedbe57fda4925c5e"
SCRIPT_NAME = "rapid-fog-4df0"
GITHUB_REPO = "thom9969-cell/unfiltered"
COMPATIBILITY_DATE = "2026-09-18"

# Files to sync to GitHub and Cloudflare
SITE_ASSETS = ["index.html"]
REPO_FILES = ["index.html", "README.md", "wrangler.jsonc", "deploy.py", ".gitignore"]


def load_credentials():
    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    gh_token = os.environ.get("GITHUB_TOKEN")
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            cf_token = cf_token or data.get("cloudflare_api_token")
            gh_token = gh_token or data.get("github_token")
    return cf_token, gh_token


def http_json(method, url, headers, body_bytes=None):
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def push_to_github(gh_token, commit_msg="Update website"):
    print(f"==> Syncing files to GitHub ({GITHUB_REPO})...")
    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Unfiltered-Deploy-Agent",
        "Content-Type": "application/json",
    }
    for rel_path in REPO_FILES:
        full_path = os.path.join(PROJECT_DIR, rel_path)
        if not os.path.exists(full_path):
            continue
        with open(full_path, "rb") as f:
            raw = f.read()
        b64 = base64.b64encode(raw).decode("ascii")

        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{rel_path}"
        status, existing = http_json("GET", url, headers)
        payload = {
            "message": f"{commit_msg} ({rel_path})",
            "content": b64,
            "branch": "main",
        }
        if status == 200 and isinstance(existing, dict) and "sha" in existing:
            # Skip if content hasn't changed
            existing_b64 = existing.get("content", "").replace("\n", "")
            if existing_b64 == b64:
                print(f"  - {rel_path}: already up to date on GitHub")
                continue
            payload["sha"] = existing["sha"]

        put_status, res = http_json(
            "PUT", url, headers, json.dumps(payload).encode("utf-8")
        )
        if put_status in (200, 201):
            print(f"  - {rel_path}: pushed to GitHub (HTTP {put_status})")
        else:
            print(f"  ! Failed to push {rel_path} to GitHub: HTTP {put_status} {res}")


def deploy_to_cloudflare(cf_token):
    print(f"==> Deploying static assets to Cloudflare ({SCRIPT_NAME} -> unfilteredadvisoryllc.com)...")
    cf_headers = {
        "Authorization": f"Bearer {cf_token}",
        "Content-Type": "application/json",
    }

    manifest = {}
    files_by_hash = {}
    for rel_path in SITE_ASSETS:
        full_path = os.path.join(PROJECT_DIR, rel_path)
        with open(full_path, "rb") as f:
            content = f.read()
        ext = os.path.splitext(rel_path)[1].lstrip(".")
        b64_content = base64.b64encode(content).decode("ascii")
        file_hash = hashlib.sha256((b64_content + ext).encode("utf-8")).hexdigest()[:32]
        manifest["/" + rel_path.lstrip("/")] = {
            "hash": file_hash,
            "size": len(content),
        }
        files_by_hash[file_hash] = (rel_path, b64_content)

    # 1. Start upload session
    session_url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/workers/scripts/{SCRIPT_NAME}/assets-upload-session"
    status, session_res = http_json(
        "POST", session_url, cf_headers, json.dumps({"manifest": manifest}).encode("utf-8")
    )
    if status != 200 or not session_res.get("success"):
        print(f"  ! Failed to start Cloudflare upload session: {status} {session_res}")
        sys.exit(1)

    jwt = session_res["result"]["jwt"]
    buckets = session_res["result"].get("buckets") or []

    # 2. Upload any new file buckets
    for bucket in buckets:
        boundary = f"----CloudflareAssetBoundary{uuid.uuid4().hex}"
        parts = []
        for h in bucket:
            rel_path, b64_data = files_by_hash[h]
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{h}"; filename="{h}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
                f"{b64_data}\r\n"
            )
        parts.append(f"--{boundary}--\r\n")
        multipart_body = "".join(parts).encode("utf-8")

        upload_url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/workers/assets/upload?base64=true"
        up_headers = {
            "Authorization": f"Bearer {jwt}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        up_status, up_res = http_json("POST", upload_url, up_headers, multipart_body)
        if up_status not in (200, 201) or not up_res.get("success"):
            print(f"  ! Asset upload failed: {up_status} {up_res}")
            sys.exit(1)
        if up_res["result"].get("jwt"):
            jwt = up_res["result"]["jwt"]
        print(f"  - Uploaded {len(bucket)} changed asset(s)")

    # 3. Finalize Worker deployment with completion JWT
    boundary = f"----CloudflareWorkerBoundary{uuid.uuid4().hex}"
    metadata = {
        "compatibility_date": COMPATIBILITY_DATE,
        "assets": {
            "jwt": jwt,
        },
    }
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="metadata"\r\n'
        f"Content-Type: application/json\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    put_url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/workers/scripts/{SCRIPT_NAME}"
    put_headers = {
        "Authorization": f"Bearer {cf_token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    put_status, put_res = http_json("PUT", put_url, put_headers, body)
    if put_status == 200 and put_res.get("success"):
        print("  - Successfully deployed to https://unfilteredadvisoryllc.com/ !")
    else:
        print(f"  ! Failed to finalize Worker deployment: {put_status} {put_res}")
        sys.exit(1)


if __name__ == "__main__":
    cf_token, gh_token = load_credentials()
    msg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Update website"
    if gh_token:
        push_to_github(gh_token, msg)
    if cf_token:
        deploy_to_cloudflare(cf_token)
