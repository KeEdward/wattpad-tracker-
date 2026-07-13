#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wattpad 連載數據自動追蹤腳本
============================
每次執行會：
1. 透過 Wattpad 公開 API 抓取「已發布的所有 part」的 reads / votes / comments（精確整數）
2. 抓取帳號與作品層級數據（follower、總 reads、總 votes、已發布 part 數等）
3. 將結果附加到兩個 CSV：data/parts.csv（一列一個 part）、data/story.csv（一列一次快照）

只使用 Python 標準函式庫，不需要安裝任何套件。
"""

import csv
import json
import os
import random
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ==================== 設定區（可直接在 GitHub 網頁上編輯） ====================

# 作品網址
STORY_URL = "https://www.wattpad.com/story/412212904-planet-phoenix-bible"

# 第 1 章發布時間（錨點，ISO 8601 含時區；+08:00 = 台灣時間）
# 第 N 章的預定發布時刻 = 錨點 + (N-1) x INTERVAL_HOURS 小時
ANCHOR_TIME = "2026-07-11T14:00:00+08:00"

# 每隔幾小時發布一章
INTERVAL_HOURS = 14

# 快照時間戳使用的時區（IANA 時區名稱）
TIMEZONE = "Asia/Taipei"

# CSV 輸出路徑（相對於 repo 根目錄）
PARTS_CSV = "data/parts.csv"
STORY_CSV = "data/story.csv"

# 請求之間的隨機延遲範圍（秒），對伺服器客氣一點
MIN_DELAY, MAX_DELAY = 1.0, 3.0

# ============================================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

PARTS_HEADER = [
    "snapshot_time",        # 快照時間戳（ISO 8601 含時區）
    "part_number",          # part 編號（依作品內順序，從 1 開始）
    "part_id",              # Wattpad 內部 part ID
    "part_title",           # part 標題
    "part_url",             # part 網址
    "reads",                # 累計閱讀數（精確整數，來自 API）
    "votes",                # 投票數（精確整數）
    "comments",             # 留言數（精確整數）
    "hours_since_publish",  # 章齡：依錨點推算的發布時刻至今經過的小時數（可為小數）
    "actual_publish_time",  # Wattpad 記錄的實際發布時間（UTC，供對照）
]

STORY_HEADER = [
    "snapshot_time",              # 快照時間戳（與 parts.csv 相同，用來關聯）
    "username",                   # 帳號名稱
    "followers",                  # follower 數
    "story_total_reads",          # 整部作品總 reads
    "story_total_votes",          # 整部作品總 votes
    "story_total_comments",       # 整部作品總 comments
    "parts_published",            # 已發布 part 數
    "account_votes_received",     # 帳號累計收到的 votes
    "account_stories_published",  # 帳號已發布作品數
]


def fetch_json(url: str) -> dict:
    """以合理的 User-Agent 抓取 JSON，失敗時重試 3 次。"""
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"抓取失敗：{url}（{last_err}）")


def polite_delay() -> None:
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def append_rows(path: str, header: list, rows: list) -> None:
    """附加資料列到 CSV；檔案不存在時先寫入標題列。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    # 只在建立新檔時寫入 BOM（讓 Excel 直接開啟不亂碼）；附加時用純 utf-8，避免 BOM 混入資料中
    encoding = "utf-8-sig" if new_file else "utf-8"
    with open(path, "a", newline="", encoding=encoding) as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(header)
        w.writerows(rows)


def already_snapshotted(path: str, snapshot_ts: str) -> bool:
    """防止同一個時間戳被重複寫入（例如同一次執行意外跑兩遍）。"""
    if not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            tail = f.read().decode("utf-8", errors="ignore")
        return snapshot_ts in tail
    except OSError:
        return False


def main() -> int:
    m = re.search(r"/story/(\d+)", STORY_URL)
    if not m:
        print(f"錯誤：無法從 STORY_URL 解析作品 ID：{STORY_URL}")
        return 1
    story_id = m.group(1)

    tz = ZoneInfo(TIMEZONE)
    snapshot_ts = datetime.now(tz).isoformat(timespec="seconds")
    anchor = datetime.fromisoformat(ANCHOR_TIME)
    now = datetime.now(tz)

    # --- 1. 作品 + 各 part 數據（一個 API 請求就能拿到全部，最省流量） ---
    story_api = (
        f"https://www.wattpad.com/api/v3/stories/{story_id}"
        "?fields=id,title,readCount,voteCount,commentCount,numParts,"
        "user(name),parts(id,title,url,readCount,voteCount,commentCount,createDate,draft)"
    )
    story = fetch_json(story_api)

    parts = [p for p in story.get("parts", []) if not p.get("draft")]
    if not parts:
        print("警告：目前沒有任何已發布的 part（排程尚未開始？），本次不寫入 part 資料。")

    if already_snapshotted(PARTS_CSV, snapshot_ts) or already_snapshotted(STORY_CSV, snapshot_ts):
        print(f"時間戳 {snapshot_ts} 已存在，跳過本次寫入以避免重複。")
        return 0

    part_rows = []
    for i, p in enumerate(parts, start=1):
        scheduled = anchor + timedelta(hours=(i - 1) * INTERVAL_HOURS)
        age_hours = round((now - scheduled).total_seconds() / 3600, 2)
        part_rows.append([
            snapshot_ts,
            i,
            p.get("id", ""),
            p.get("title", ""),
            p.get("url", ""),
            p.get("readCount", ""),
            p.get("voteCount", ""),
            p.get("commentCount", ""),
            age_hours,
            p.get("createDate", ""),
        ])

    # --- 2. 帳號層級數據 ---
    username = (story.get("user") or {}).get("name", "")
    followers = votes_received = stories_published = ""
    if username:
        polite_delay()
        try:
            user = fetch_json(
                f"https://www.wattpad.com/api/v3/users/{username}"
                "?fields=username,numFollowers,votesReceived,numStoriesPublished"
            )
            followers = user.get("numFollowers", "")
            votes_received = user.get("votesReceived", "")
            stories_published = user.get("numStoriesPublished", "")
        except RuntimeError as e:
            print(f"警告：帳號數據抓取失敗，本欄留空。{e}")

    story_row = [[
        snapshot_ts,
        username,
        followers,
        story.get("readCount", ""),
        story.get("voteCount", ""),
        story.get("commentCount", ""),
        len(parts),
        votes_received,
        stories_published,
    ]]

    # --- 3. 寫入 CSV ---
    if part_rows:
        append_rows(PARTS_CSV, PARTS_HEADER, part_rows)
    append_rows(STORY_CSV, STORY_HEADER, story_row)

    print(f"完成：{snapshot_ts} 已寫入 {len(part_rows)} 個 part、1 列作品快照。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
