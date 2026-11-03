#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WhaleFlow 云端静态数据构建器 (供 GitHub Actions / 云端定时任务直接运行)
功能：拉取 whales_100_config.json 中的机构最新 SEC 13F 原始底稿 -> 算好真实股数差分 -> 覆写 cards_feed.json
"""

import json
import time
from pathlib import Path
from whale_server import CONFIG_PATH, CACHE_FEED_PATH, diff_two_quarters


def build_static_feed():
    if not CONFIG_PATH.exists():
        print("未找到 whales_100_config.json")
        return

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    institutions = cfg.get("institutions", [])
    cards = []

    print(f"开始拉取并差分 {len(institutions)} 家顶级机构的 SEC EDGAR 13F 底稿...")
    for idx, inst in enumerate(institutions, 1):
        try:
            print(f"[{idx}/{len(institutions)}] 正在穿透: {inst['name']} (CIK: {inst['cik']})...")
            card = diff_two_quarters(inst)
            if card:
                cards.append(card)
        except Exception as e:
            print(f"  -> 跳过 {inst['name']}: {e}")

    payload = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "total_cards": len(cards),
        "cards": cards
    }
    CACHE_FEED_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"构建完成！共生成 {len(cards)} 张高信噪比图卡，已写入 {CACHE_FEED_PATH.name}")


if __name__ == "__main__":
    build_static_feed()
