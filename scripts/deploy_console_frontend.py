"""Deploy only built existing console assets through normal Amplify manual hosting.

Fixed approved account/app; no public source push or Git-provider credential.
Presigned upload URLs stay in memory and are never printed or persisted.

Repeatable path (needs the cyclotron-production SSO profile):

    npm --prefix trace-ui ci && npm --prefix trace-ui run build
    node trace-ui/scripts/package-console.mjs
    python scripts/deploy_console_frontend.py            # dry run: checks and prints what would ship
    python scripts/deploy_console_frontend.py --apply    # upload, start, wait, verify the live page

It refuses a dirty tree or a built bundle that differs from the committed vendor bundle, so what ships is
exactly a commit. After the job succeeds it fetches the live index.html and asset hashes and compares them.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import time
import zipfile
import httpx

APP = "d2lya5yok0779c"
SITE = "https://lab.cyclotron.anth.us"
ROOT = Path(__file__).resolve().parents[1]


def aws(*args):
    result = subprocess.run(
        [
            "aws",
            *args,
            "--profile",
            "cyclotron-production",
            "--region",
            "us-east-1",
            "--output",
            "json",
            "--no-cli-pager",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(
            "Amplify deployment action failed; inspect scoped AWS action separately"
        )
    return json.loads(result.stdout)


def git(*args):
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def built():
    dist = ROOT / "trace-ui/console-dist"
    assert (dist / "index.html").is_file(), (
        "run trace-ui build and package-console.mjs first"
    )
    vendor = ROOT / "src/decision_flywheel/vendor/trace-ui"
    for name in ("web.js", "web.css"):
        assert (dist / "assets" / name).read_bytes() == (vendor / name).read_bytes(), (
            f"console-dist {name} differs from the committed vendor bundle"
        )
    assert not git("status", "--porcelain", "--untracked-files=no"), (
        "commit or stash local changes first"
    )
    return dist


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="upload and start the deployment (default: dry run)",
    )
    args = parser.parse_args()
    dist = built()
    files = {
        path.relative_to(dist).as_posix(): path.read_bytes()
        for path in sorted(dist.rglob("*"))
        if path.is_file()
    }
    plan = {
        "appId": APP,
        "commit": git("rev-parse", "HEAD"),
        "files": {
            name: hashlib.sha256(data).hexdigest()[:16] for name, data in files.items()
        },
    }
    assert aws("sts", "get-caller-identity")["Account"] == "682849160744"
    app = aws("amplify", "get-app", "--app-id", APP)["app"]
    assert app["name"] == "cyclotron-console" and app["platform"] == "WEB"
    if not args.apply:
        print(json.dumps({**plan, "dryRun": True}, indent=1))
        return
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    deployment = aws(
        "amplify", "create-deployment", "--app-id", APP, "--branch-name", "production"
    )
    # A created but never started deployment blocks the next one, so cancel it if the upload fails.
    try:
        response = httpx.put(
            deployment["zipUploadUrl"],
            content=data.getvalue(),
            timeout=60,
            follow_redirects=False,
        )
        if response.status_code not in (200, 201):
            raise RuntimeError("Amplify artifact upload rejected")
    except Exception:
        aws(
            "amplify",
            "stop-job",
            "--app-id",
            APP,
            "--branch-name",
            "production",
            "--job-id",
            deployment["jobId"],
        )
        raise
    job = aws(
        "amplify",
        "start-deployment",
        "--app-id",
        APP,
        "--branch-name",
        "production",
        "--job-id",
        deployment["jobId"],
    )["jobSummary"]["jobId"]
    for _ in range(60):
        status = aws(
            "amplify",
            "get-job",
            "--app-id",
            APP,
            "--branch-name",
            "production",
            "--job-id",
            job,
        )["job"]["summary"]["status"]
        if status not in ("PENDING", "PROVISIONING", "RUNNING", "CREATED"):
            break
        time.sleep(5)
    if status != "SUCCEED":
        raise RuntimeError(f"Amplify job {job} ended {status}")
    live = {}
    for _ in range(12):
        page = httpx.get(
            f"{SITE}/index.html", headers={"Cache-Control": "no-cache"}, timeout=20
        ).text
        live = {
            name: hashlib.sha256(
                httpx.get(f"{SITE}/{name}", timeout=20).content
            ).hexdigest()[:16]
            for name in plan["files"]
            if name.startswith("assets/")
        }
        if page == files["index.html"].decode() and all(
            live[name] == plan["files"][name] for name in live
        ):
            break
        time.sleep(10)
    else:
        raise RuntimeError(
            "live page does not yet serve the deployed files; check the CDN cache"
        )
    print(
        json.dumps(
            {
                **plan,
                "jobId": job,
                "status": status,
                "liveVerified": True,
                "sourcePushed": False,
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
