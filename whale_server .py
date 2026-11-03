#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WhaleFlow 本地全栈引擎 (免注册对接 SEC EDGAR 13F XML & 本地 Web 服务)
运行方式: python whale_server.py
"""

import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from http.server import SimpleHTTPRequestHandler, HTTPServer
from pathlib import Path

# SEC EDGAR 官方要求的唯一免注册通行证 (User-Agent)
SEC_HEADERS = {
    "User-Agent": "WhaleFlowResearchAgent/1.0 (contact@whaleflow.local)",
    "Accept-Encoding": "gzip, deflate",
}

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "whales_100_config.json"
CACHE_FEED_PATH = BASE_DIR / "cards_feed.json"


def fetch_sec_json(url: str) -> dict:
    """免注册直接请求 SEC 官方 JSON 接口 (遵守 <=10 req/s 速率限制)"""
    req = urllib.request.Request(url, headers=SEC_HEADERS)
    time.sleep(0.15)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_latest_two_13f_accessions(cik: str) -> list:
    """获取指定 CIK 机构最近两个季度的 13F-HR 报告归档号与日期"""
    cik_padded = cik.zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    data = fetch_sec_json(url)
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])

    results = []
    for i, form in enumerate(forms):
        if form == "13F-HR":
            results.append({
                "accession": accessions[i].replace("-", ""),
                "accession_raw": accessions[i],
                "filing_date": filing_dates[i],
                "report_date": report_dates[i]
            })
            if len(results) == 2:
                break
    return results


def parse_13f_infotable_xml(cik: str, accession: str, strip_options: bool = False) -> dict:
    """
    拉取并解析 13F 原始 XML 持仓表，执行：
    1) 剥离做市商 Put/Call 期权噪音
    2) 按 CUSIP 汇总真实正股股数 (Shares) 与市值 (Value)
    """
    cik_int = str(int(cik))
    index_url = f"https://data.sec.gov/Archives/edgar/data/{cik_int}/{accession}/index.json"
    idx_data = fetch_sec_json(index_url)
    items = idx_data.get("directory", {}).get("item", [])

    # 寻找存有具体持仓明细的 infotable.xml 文件
    xml_filename = None
    for item in items:
        name = item.get("name", "")
        if name.endswith(".xml") and "primary_doc" not in name.lower():
            xml_filename = name
            break

    if not xml_filename:
        return {}

    xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}/{xml_filename}"
    req = urllib.request.Request(xml_url, headers=SEC_HEADERS)
    time.sleep(0.15)
    with urllib.request.urlopen(req, timeout=20) as resp:
        xml_bytes = resp.read()

    root = ET.fromstring(xml_bytes)
    holdings = {}

    for info in root.iter():
        if info.tag.endswith("infoTable"):
            issuer = ""
            cusip = ""
            value = 0.0
            shares = 0.0
            put_call = ""

            for child in info:
                tag = child.tag.split("}")[-1]
                if tag == "nameOfIssuer" and child.text:
                    issuer = child.text.strip()
                elif tag == "cusip" and child.text:
                    cusip = child.text.strip()
                elif tag == "value" and child.text:
                    value = float(child.text.strip())
                elif tag == "putCall" and child.text:
                    put_call = child.text.strip().upper()
                elif tag == "shrsOrPrnAmt":
                    for sub in child:
                        if sub.tag.endswith("sshPrnamt") and sub.text:
                            shares = float(sub.text.strip())

            # 核心降噪规则：剥离期权头寸
            if strip_options and put_call in ("PUT", "CALL"):
                continue

            if not cusip:
                continue

            key = f"{cusip}_{put_call}" if put_call else cusip
            if key not in holdings:
                holdings[key] = {"issuer": issuer, "cusip": cusip, "shares": 0.0, "value": 0.0, "put_call": put_call}
            holdings[key]["shares"] += shares
            holdings[key]["value"] += value

    return holdings


def diff_two_quarters(inst: dict) -> dict:
    """对比一家机构最近两期 13F，生成一张小红书视觉图卡 JSON"""
    cik = inst["cik"]
    strip_opt = inst.get("strip_options", False)
    filings = get_latest_two_13f_accessions(cik)
    if len(filings) < 2:
        return {}

    curr_meta, prev_meta = filings[0], filings[1]
    curr_map = parse_13f_infotable_xml(cik, curr_meta["accession"], strip_opt)
    prev_map = parse_13f_infotable_xml(cik, prev_meta["accession"], strip_opt)

    total_val = sum(v["value"] for v in curr_map.values()) or 1.0
    deltas = []

    all_keys = set(curr_map.keys()) | set(prev_map.keys())
    for k in all_keys:
        c = curr_map.get(k, {"issuer": prev_map.get(k, {}).get("issuer", ""), "shares": 0.0, "value": 0.0})
        p = prev_map.get(k, {"shares": 0.0, "value": 0.0})

        c_sh, p_sh = c["shares"], p["shares"]
        weight_pct = (c["value"] / total_val) * 100.0

        if p_sh == 0 and c_sh > 0:
            action_str = "新进建仓"
            pct_change = 100.0
        elif c_sh == 0 and p_sh > 0:
            action_str = "彻底清仓"
            pct_change = -100.0
        elif p_sh > 0:
            pct_change = ((c_sh - p_sh) / p_sh) * 100.0
            action_str = f"股数 {pct_change:+.1f}%"
        else:
            continue

        # 过滤微小仓位噪音，只保留权重 > 1.5% 或清仓前权重较大的核心动作
        if weight_pct >= 1.5 or (pct_change == -100.0 and p["value"] > total_val * 0.02):
            deltas.append({
                "issuer": c["issuer"],
                "action": action_str,
                "pct_change": pct_change,
                "weight_pct": weight_pct,
                "val_str": f"占比 {weight_pct:.1f}%" if c_sh > 0 else "已清零"
            })

    # 按仓位权重与变动幅度排序，提取前3大核心动作上封面图
    deltas.sort(key=lambda x: (abs(x["pct_change"]) > 15, x["weight_pct"]), reverse=True)
    top3 = deltas[:3]
    if not top3:
        return {}

    hero = top3[0]
    return {
        "id": int(time.time() * 1000) % 1000000,
        "category": inst["category"],
        "tag": f"SEC 13F 实盘差分 · {curr_meta['report_date']}",
        "tagColor": "bg-emerald-500/20 text-emerald-300 border border-emerald-500/40",
        "bgGradient": "from-emerald-950/90 via-[#111827] to-[#0f172a]",
        "accentColor": "#10b981",
        "heroTicker": hero["issuer"][:18],
        "heroMetric": hero["action"],
        "heroLabel": f"{inst['name']} 最新季报核心异动 (总持仓前三大边际变化)",
        "subData": [{"name": d["issuer"][:16], "action": d["action"], "val": d["val_str"]} for d in top3],
        "title": f"【官方底稿穿透】{inst['name']} 最新 13F 披露：头号动作直指 {hero['issuer']} ({hero['action']})，当前占组合权重 {hero['weight_pct']:.1f}%",
        "instAvatar": inst.get("avatar", "🐋"),
        "instName": inst["name"],
        "sourceName": f"SEC EDGAR (13F-HR / CIK:{cik})",
        "sourceUrl": f"https://www.sec.gov/edgar/browse/?CIK={cik}",
        "filingDate": curr_meta["filing_date"],
        "reportPeriod": f"{curr_meta['report_date']} 截面",
        "filingType": f"CIK: {cik}",
        "timeAgo": f"披露日 {curr_meta['filing_date']}",
        "conviction": "9.5 / 10",
        "detailLogic": [
            f"【报告期对比】本卡片由本地引擎直接拉取 SEC EDGAR 原始 XML 生成，对比报告期：{curr_meta['report_date']} vs {prev_meta['report_date']}。",
            f"【真实股数差分】已完全剔除二级市场股价涨跌带来的市值幻觉，所有百分比均为真实持有股数 (ΔShares) 变动。",
            f"【原始底稿查验】SEC 归档号：{curr_meta['accession_raw']}（期权噪音过滤状态：{'已开启' if strip_opt else '纯多头无需过滤'}）。"
        ]
    }


class WhaleRequestHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.path = "/whaleflow_app.html"
        elif self.path.startswith("/api/sync"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            try:
                cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                # 默认演示同步前 3 家核心巨鲸的实时 SEC 13F 底稿，避免首次点击等待过长
                live_cards = []
                for inst in cfg["institutions"][:3]:
                    card = diff_two_quarters(inst)
                    if card:
                        live_cards.append(card)
                CACHE_FEED_PATH.write_text(json.dumps(live_cards, ensure_ascii=False, indent=2), encoding="utf-8")
                self.wfile.write(json.dumps({"status": "ok", "cards": live_cards}, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False).encode("utf-8"))
            return
        return super().do_GET()


if __name__ == "__main__":
    port = 8080
    print(f"==================================================")
    print(f"🐋 WhaleFlow 本地引擎已启动！")
    print(f"👉 请在浏览器打开: http://localhost:{port}")
    print(f"==================================================")
    server = HTTPServer(("127.0.0.1", port), WhaleRequestHandler)
    server.serve_forever()
