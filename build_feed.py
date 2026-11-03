#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WhaleFlow 云端静态数据构建器 (供 GitHub Actions 定时运行)
每次运行自动聚合：
1. SEC 全网过去7天最新 13D/13G 实时举牌 (>5%股权异动)
2. 全球主权基金/对冲巨鳄/港股席位 7x24 实时调仓快讯
3. whales_100_config.json 中全部机构的最新两期 13F 实盘股数差分
"""

from whale_server import generate_full_live_feed

if __name__ == "__main__":
    payload = generate_full_live_feed(max_13f_count=20)
    print(f"✅ 构建完成！共生成 {payload['total_cards']} 张实时异动图卡，更新时间：{payload['updated_at']}")
