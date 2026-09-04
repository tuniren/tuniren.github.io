#!/usr/bin/env python3
import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def executable(name: str) -> str:
    resolved = shutil.which(name)
    if resolved:
        return resolved
    if sys.platform == "win32" and not name.lower().endswith((".exe", ".cmd", ".bat")):
        for suffix in (".cmd", ".exe", ".bat"):
            resolved = shutil.which(f"{name}{suffix}")
            if resolved:
                return resolved
    return name


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = [executable(args[0]), *args[1:]]
    print(f"$ {' '.join(args)}", flush=True)
    return subprocess.run(command, cwd=ROOT, text=True, check=check)


def capture(args: list[str]) -> str:
    command = [executable(args[0]), *args[1:]]
    return subprocess.check_output(command, cwd=ROOT, text=True).strip()


def git_args(args: list[str], proxy: str | None = None) -> list[str]:
    if not proxy:
        return ["git", *args]
    return ["git", "-c", f"http.proxy={proxy}", "-c", f"https.proxy={proxy}", *args]


def has_staged_changes() -> bool:
    result = subprocess.run(
        [executable("git"), "diff", "--cached", "--quiet"],
        cwd=ROOT,
        text=True,
    )
    return result.returncode != 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建、提交并发布博客站到 GitHub Pages。")
    parser.add_argument(
        "-m",
        "--message",
        default="feat: 发布技术博客站\n\n- 搭建 Astro Markdown 技术博客\n- 添加 GitHub Pages 自动部署\n- 加入 Harvester Longhorn 恢复文章",
        help="Git 提交信息。建议使用中文 Conventional Commit 格式。",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="发布分支。默认使用当前分支。",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="Git 远端名称，默认为 origin。",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help="Git 网络访问使用的代理，例如 http://127.0.0.1:7890。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    branch = args.branch or capture(["git", "branch", "--show-current"])
    if not branch:
        print("无法识别当前分支，请通过 --branch 显式指定。", file=sys.stderr)
        return 1

    run(["npm", "run", "build"])
    run(git_args(["add", "-A"]))
    run(git_args(["diff", "--cached", "--check"]))

    if has_staged_changes():
        run(git_args(["commit", "-m", args.message]))
    else:
        print("没有新的暂存变更，继续同步并推送当前分支。")

    run(git_args(["pull", "--rebase", "--autostash", args.remote, branch], args.proxy))
    run(git_args(["push", args.remote, branch], args.proxy))
    run(git_args(["fetch", args.remote, branch], args.proxy))

    local_head = capture(["git", "rev-parse", "HEAD"])
    remote_head = capture(["git", "rev-parse", f"{args.remote}/{branch}"])
    if local_head != remote_head:
        print(f"发布校验失败：本地 HEAD {local_head} 与远端 {args.remote}/{branch} {remote_head} 不一致。", file=sys.stderr)
        return 1

    print(f"发布完成：{args.remote}/{branch} 已同步到 {local_head}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
