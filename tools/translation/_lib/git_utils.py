#!/usr/bin/env python3
"""Git 操作ラッパーモジュール。

翻訳同期ツールが使用する Git コマンドのラッパー関数を提供する。
すべての関数は subprocess を使用して Git コマンドを実行し、
リポジトリルートから操作を行う。
"""

from __future__ import annotations

import subprocess


def git_show(ref: str, path: str) -> str | None:
    """指定された Git ref のファイル内容をテキストとして取得する。

    ``git show <ref>:<path>`` を実行し、ファイル内容を文字列で返す。

    Args:
        ref: Git の参照 (ブランチ名、コミット SHA 等)。
        path: リポジトリルートからの相対パス。

    Returns:
        ファイル内容の文字列。ファイルが存在しない場合は None。
    """
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError:
        return None


def git_show_binary(ref: str, path: str) -> bytes | None:
    """指定された Git ref のファイル内容をバイナリとして取得する。

    ``git show <ref>:<path>`` を実行し、ファイル内容を生バイトで返す。
    画像などのバイナリファイルに使用する。

    Args:
        ref: Git の参照 (ブランチ名、コミット SHA 等)。
        path: リポジトリルートからの相対パス。

    Returns:
        ファイル内容のバイト列。ファイルが存在しない場合は None。
    """
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            capture_output=True,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError:
        return None


def git_fetch(remote: str = "upstream") -> None:
    """指定されたリモートから最新の情報をフェッチする。

    ``git fetch <remote>`` を実行する。

    Args:
        remote: リモート名。デフォルトは ``"upstream"``。

    Raises:
        RuntimeError: フェッチに失敗した場合。
    """
    result = subprocess.run(
        ["git", "fetch", remote],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git fetch {remote} failed: {result.stderr.strip()}"
        )


def git_rev_parse(ref: str) -> str:
    """指定された参照のフルコミット SHA を取得する。

    ``git rev-parse <ref>`` を実行し、完全な SHA を返す。

    Args:
        ref: Git の参照 (ブランチ名、タグ名、短縮 SHA 等)。

    Returns:
        フルコミット SHA 文字列。

    Raises:
        RuntimeError: 参照の解決に失敗した場合。
    """
    result = subprocess.run(
        ["git", "rev-parse", ref],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git rev-parse {ref} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def git_ls_tree(ref: str, path: str = "modules/") -> list[str]:
    """指定された ref のファイル一覧を取得する。

    ``git ls-tree -r --name-only <ref> <path>`` を実行し、
    ファイルパスのリストを返す。

    Args:
        ref: Git の参照。
        path: 一覧を取得するディレクトリパス。デフォルトは ``"modules/"``。

    Returns:
        ファイルパスの文字列リスト。該当ファイルがない場合は空リスト。
    """
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref, path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    output = result.stdout.strip()
    if not output:
        return []
    return output.split("\n")


def git_remote_exists(remote: str) -> bool:
    """指定された名前の Git リモートが存在するかを確認する。

    ``git remote`` を実行し、出力に指定されたリモート名が含まれるか確認する。

    Args:
        remote: 確認するリモート名。

    Returns:
        リモートが存在すれば True、存在しなければ False。
    """
    result = subprocess.run(
        ["git", "remote"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    remotes = result.stdout.strip().split("\n")
    return remote in remotes


def git_status_clean() -> bool:
    """作業ディレクトリがクリーン (未コミットの変更なし) かを確認する。

    ``git status --porcelain`` を実行し、出力が空かどうかを返す。

    Returns:
        作業ディレクトリがクリーンなら True、変更があれば False。
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == ""


def git_switch_create_branch(branch: str, start_point: str = "main") -> None:
    """新しいブランチを作成して切り替える。

    ``git switch -c <branch> <start_point>`` を実行する。

    Args:
        branch: 作成するブランチ名。
        start_point: ブランチの起点。デフォルトは ``"main"``。

    Raises:
        RuntimeError: ブランチの作成・切り替えに失敗した場合。
    """
    result = subprocess.run(
        ["git", "switch", "-c", branch, start_point],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git switch -c {branch} {start_point} failed: {result.stderr.strip()}"
        )


def git_branch_exists(branch: str) -> bool:
    """指定された名前のローカルブランチが存在するかを確認する。

    ``git rev-parse --verify refs/heads/<branch>`` を実行して確認する。

    Args:
        branch: 確認するブランチ名。

    Returns:
        ブランチが存在すれば True、存在しなければ False。
    """
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def git_current_branch() -> str:
    """現在のブランチ名を取得する。

    ``git rev-parse --abbrev-ref HEAD`` を実行し、現在のブランチ名を返す。

    Returns:
        現在のブランチ名の文字列。
    """
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
