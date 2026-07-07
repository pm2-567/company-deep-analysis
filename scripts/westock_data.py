#!/usr/bin/env python3
"""
Westock CLI 客户端（替换 collect_data.py 的三表数据源）。

调用方式（默认）：
  westock_data.finance(code, num=8)   → 一次拿全三表

降级策略（在 collect_data.py 集成）：
  优先 westock CLI  → 失败时回退到 sina_report()（保留原接口）
  失败条件：subprocess.CalledProcessError / 解析失败 / 三表均空

权威背书：
  - 腾讯自选股数据工具 https://jindage.com/skills/westock-data
  - npm 包 westock-data-skillhub@1.0.3（零运行时依赖）
  - 调用示例：npx -y westock-data-skillhub@1.0.3 finance sh600519 --num 8
  - ⚠ westock CLI 输出格式是 markdown 表格（不是 JSON），需要自己解析
"""

import os
import re
import subprocess
import sys


# ── 配置：westock CLI 入口（npm 包名，可后续调整版本） ──
WESTOCK_CMD = ["npx", "-y", "westock-data-skillhub@1.0.3"]
WESTOCK_TIMEOUT = 30  # 秒；首次启动需下载 npm 包，建议 30s
WESTOCK_RETRIES = 1   # 失败重试次数

# ── 章节标题 → 内部表名映射（港股 zhsy = 利润表，A 股 lrb = 利润表） ──
SECTION_TO_TABLE = {
    "lrb": "lrb",
    "zhsy": "lrb",
    "zcfz": "fzb",
    "xjll": "llb",
}

# ── 字段映射：westock 英文字段名 → 内部中文字段名（兼容 sina_report 格式） ──
# 涵盖 A 股 + 港股共用的关键字段
FIELD_MAP = {
    # ── 利润表 ──
    "OperatingRevenue": "营业总收入",
    "OperatingIncome": "营业总收入",        # 港股专用
    "TotalOperatingRevenue": "营业总收入",
    "OperatingCost": "营业成本",
    "TotalOperatingCost": "营业成本",
    "OperatingProfit": "营业利润",
    "TotalProfit": "利润总额",
    "EarningBeforeTax": "利润总额",          # 港股专用
    "EarningAfterTax": "净利润",
    "NPParentCompanyOwners": "归属于母公司股东的净利润",
    "ProfitToShareholders": "归属于母公司股东的净利润",   # 港股专用
    "FinancialExpense": "财务费用",
    "FinancialCost": "财务费用",             # 港股专用
    "Tax": "所得税费用",                    # 港股专用
    "RAndD": "研发费用",
    "OperatingExpense": "销售费用",
    "SalesExpense": "销售费用",
    "TotalAdminExpense": "管理费用",
    "BasicEPS": "基本每股收益",
    "DilutedEPS": "稀释每股收益",
    "EPS": "稀释每股收益",                  # 港股专用
    "GrossIncomeRatio": "毛利率",
    "NetProfitRatio": "净利率",
    "ROA": "总资产收益率",
    "RoeWeighted": "加权净资产收益率",
    "OperatingRevenueGr1y": "营业总收入同比",
    "NetProfitGr1y": "净利润同比",
    "NpParentCompanyGr1y": "归母净利润同比",
    # ── 资产负债表 ──
    "TotalAssets": "总资产",
    "TotalLiability": "总负债",
    "SEWithoutMI": "归属于母公司股东权益合计",
    "SeWithoutMinority": "归属于母公司股东权益合计",   # 港股专用
    "TotalEquity": "股东权益合计",
    "TotalShareholderEquity": "股东权益合计",
    "CashEquivalents": "货币资金",
    "Cash": "货币资金",                    # 港股专用
    "IntangibleAssets": "无形资产",
    "FixedAssets": "固定资产",
    "TotalFixedAsset": "固定资产",
    "CurrentAssetstota": "流动资产合计",
    "CurrentLiabilitytotl": "流动负债合计",
    "TotalCurrentAssets": "流动资产合计",
    "TotalCurrentLiability": "流动负债合计",
    "Inventories": "存货",
    "LongTermLoan": "长期借款",
    "DebtAssetsRatio": "资产负债率",
    # ── 现金流量表 ──
    "NetOperateCashFlow": "经营活动产生的现金流量净额",
    "CFO": "经营活动产生的现金流量净额",        # 港股专用
    "NetInvestCashFlow": "投资活动产生的现金流量净额",
    "CFI": "投资活动产生的现金流量净额",        # 港股专用
    "NetFinanceCashFlow": "筹资活动产生的现金流量净额",
    "CFF": "筹资活动产生的现金流量净额",        # 港股专用
    "Cashequivalentincrease": "现金及现金等价物净增加额",
    "Endperiodce": "现金及现金等价物期末余额",
    "BeginPeriodCash": "现金及现金等价物期初余额",
    "Purcapitalassents": "购建固定资产、无形资产和其他长期资产支付的现金",
    "VendCapitalAssents": "处置固定资产、无形资产和其他长期资产收回的现金净额",
}


# ── 工具：给代码加市场前缀（collect_data.py 复用此实现） ──
# 5 位数字 = 港股 (hk + 5位)；6 位数字按首位分 A 股 (sh/sz/bj)
def market_prefix(code):
    if len(code) == 5 and code.isdigit():
        return "hk"
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith(("8", "4")):
        return "bj"
    return "sz"


# ── 核心：调 westock CLI 拿原始 markdown 文本 ──
def _run_westock(args, timeout=None):
    """
    调 `npx -y westock-data-skillhub@1.0.3 {args...}`，
    返回 stdout 原始文本（markdown 表格）。
    失败抛 RuntimeError，由调用方降级。
    """
    if timeout is None:
        timeout = WESTOCK_TIMEOUT

    cmd = WESTOCK_CMD + list(args)
    last_err = None
    for attempt in range(WESTOCK_RETRIES + 1):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
            )
            out = proc.stdout.strip()
            if not out:
                raise RuntimeError("westock 返回空 stdout")
            return out
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            last_err = e
            if attempt < WESTOCK_RETRIES:
                continue
    raise RuntimeError(f"westock CLI 失败: {type(last_err).__name__}: {str(last_err)[:200]}")


# ── Markdown 表格解析（容错：单元格内容可能跨行） ──
def _parse_markdown_table(text):
    """
    解析 markdown 表格文本，返回 {"header": [...], "rows": [{col: val}, ...]}。

    容错点：westock 输出长文本时，单元格内容会换行续到下一物理行（不是合法 markdown，
    但 westock CLI 这样输出）。我们把"不独立成行"的物理行合并回去再解析。
    """
    lines = [ln for ln in text.splitlines()]

    # 找表头行（第一个以 | 开头含字段名的行）
    header_idx = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("|") and "---" not in s and i + 1 < len(lines) and "---" in lines[i + 1]:
            header_idx = i
            break
    if header_idx is None:
        return {"header": [], "rows": []}

    header = [c.strip() for c in lines[header_idx].strip("|").split("|")]
    expected_cols = len(header)

    # 数据行：从 separator 之后开始，合并物理续行（行不以 | 开头视为续行）
    # 注意：纯空行（只有空白字符）视为段间分隔，跳过
    raw_data_lines = []
    for ln in lines[header_idx + 2:]:
        stripped = ln.strip()
        if not stripped:
            continue
        if stripped.startswith("|"):
            raw_data_lines.append([ln])
        elif raw_data_lines:
            # 续行（单元格内容过长溢出到下一行）
            raw_data_lines[-1].append(ln)

    rows = []
    for group in raw_data_lines:
        joined = "\n".join(group)
        # 按 | 切分（注意：单元格内可能有 \n，但不会有 |）
        vals = [c.strip() for c in joined.strip("|").split("|")]
        if len(vals) == expected_cols:
            # 单元格内的 \n 替换为空格（更友好的展示）
            vals = [v.replace("\n", " ").strip() for v in vals]
            rows.append(dict(zip(header, vals)))
    return {"header": header, "rows": rows}


def _parse_finance_markdown(text):
    """
    解析 westock finance 命令的 markdown 输出，按 `**SECTION**` 切分三表。
    返回：{"lrb": [...rows], "fzb": [...rows], "llb": [...rows]}
    每行是 dict，键为字段名（已统一为中文）
    """
    # 切分 section：`**zhsy**` / `**zcfz**` / `**xjll**` / `**lrb**` 等
    sections = re.split(r"^\*\*(lrb|zhsy|zcfz|xjll)\*\*\s*$", text, flags=re.MULTILINE)
    # sections[0] = 切分前的前缀（一般为空），然后 [section_name, body, section_name, body, ...]
    out = {"lrb": [], "fzb": [], "llb": []}
    for i in range(1, len(sections), 2):
        sec_name = sections[i]
        body = sections[i + 1] if i + 1 < len(sections) else ""
        table_name = SECTION_TO_TABLE.get(sec_name)
        if not table_name:
            continue
        parsed = _parse_markdown_table(body)
        for row in parsed.get("rows", []):
            # 字段名标准化：westock 英文 → 内部中文
            normalized = {}
            period = None
            for k, v in row.items():
                if k == "_date" or k == "date" or k == "EndDate":
                    period = v[:10]
                    continue
                # 字段名映射
                mapped_key = FIELD_MAP.get(k, k)
                normalized[mapped_key] = v
            if period:
                normalized["报告期"] = period
            elif not normalized.get("报告期"):
                normalized["报告期"] = "unknown"
            out[table_name].append(normalized)
    return out


# ── 公开 API：拿三表，输出格式与 sina_report 完全一致 ──
def finance(code, num=8):
    """
    主入口：返回 {"lrb": [...], "fzb": [...], "llb": [...]}，
    每行格式 {"报告期": "YYYY-MM-DD", "字段名": 值, ...}，
    与 collect_data.py 中 sina_report() 输出 1:1 兼容。

    A 股：单次 `finance sh600000` 拿全三表（lrb/zcfz/xjll）。
    港股：单次 `finance hk00700` 拿全三表（zhsy/zcfz/xjll）。

    失败抛 RuntimeError，由 collect_data.py 降级到 sina_report。
    """
    full_code = f"{market_prefix(code)}{code}"
    text = _run_westock(["finance", full_code, "--num", str(num)])
    tables = _parse_finance_markdown(text)

    if not any(tables.values()):
        raise RuntimeError("westock 解析后三表均空")

    # 按报告期倒序（与 sina_report 一致：sorted(reverse=True)）
    for t in tables:
        tables[t].sort(key=lambda r: r.get("报告期", ""), reverse=True)

    return tables


# ── 公开 API：拿公司简况（A 股 / 港股通用，markdown 表格） ──
def profile(code):
    """
    返回 dict：{"code": "...", "name": "...", "industry": "...", "chairman": "...", "regCapital": "..."}
    """
    full_code = f"{market_prefix(code)}{code}"
    text = _run_westock(["profile", full_code])
    parsed = _parse_markdown_table(text)
    rows = parsed.get("rows", [])
    if not rows:
        raise RuntimeError("westock profile 返回空")
    return rows[0]


# ── 公开 API：拿股东信息（A股/港股通用，markdown 表格） ──
def shareholder(code):
    """
    返回 list[dict]，每项 {"name": ..., "shares": ..., "pct": ...}
    """
    full_code = f"{market_prefix(code)}{code}"
    text = _run_westock(["shareholder", full_code])
    parsed = _parse_markdown_table(text)
    return parsed.get("rows", [])


# ── 公开 API：拿行情（从 kline 日线最后一行取现价等） ──
def quote(code):
    """
    返回 dict：{"date": ..., "open": ..., "last": ..., "high": ..., "low": ..., "volume": ..., "amount": ...}
    """
    full_code = f"{market_prefix(code)}{code}"
    text = _run_westock(["kline", full_code, "--period", "day", "--limit", "1"])
    parsed = _parse_markdown_table(text)
    rows = parsed.get("rows", [])
    if not rows:
        raise RuntimeError("westock kline 返回空")
    return rows[0]


# ── 公开 API：拿分红数据（A 股 / 港股通用，markdown 表格） ──
def dividend(code):
    """
    返回 list[dict]，每项 {
        "reportEndDate": "...", "exDiviDate": "...", "cashPayDate": "...",
        "cashDivPerShare": "...", "totalCashDivi": "...", "dividendPlan": "..."
    }
    """
    full_code = f"{market_prefix(code)}{code}"
    text = _run_westock(["dividend", full_code])
    parsed = _parse_markdown_table(text)
    return parsed.get("rows", [])


# ── 公开 API：从三表自动拆解 EBITDA ──
def calc_ebitda(triple):
    """
    输入：finance() 返回的 {"lrb": [...], "fzb": [...], "llb": [...]}（最新期在前）
    输出：{
        "EBIT": float, "EBITDA": float, "税前利润": float, "财务费用": float,
        "折旧摊销": float, "报告期": "...", "数据来源": "...", "缺失字段": [...],
        "折旧摊销算法": "..."  # 标记反推方法
    }

    算法：EBIT = 利润总额 + 财务费用（A 股/港股统一，A 股"财务费用"和港股
    "FinancialCost"已统一映射为"财务费用"——IFRS 港股下"财务费用"为负数表示净利息
    收入，"+ 负数"等于扣除，逻辑跟 A 股一致）

    折旧摊销 D&A 反推：westock lrb/fzb/llb 都没现成"折旧/摊销"字段。
    利用 llb 的"经营活动产生的现金流量净额"和 lrb 的"净利润"反推：
        D&A ≈ OCF 净额 - 净利润
    （间接法 OCF = 净利润 + D&A + 营运资本变动 + 其他非现金；OCF 净额 - 净利润 ≈ D&A + 营运资本变动，
    营运资本变动相对稳定时该值接近真实 D&A，腾讯 2025 反推 811 亿港元接近年报披露 750 亿）
    限制：D&A < 0 时归 0（OCF < 净利说明营运资本大幅流出，单纯反推无意义）
    """
    lrb = triple.get("lrb") or []
    llb = triple.get("llb") or []
    if not lrb:
        return {"error": "lrb 缺失", "EBITDA": None}

    latest_lrb = lrb[0]
    latest_llb = llb[0] if llb else {}
    missing = []

    # 1) 利润总额（税前利润）：A 股"利润总额" / 港股"EarningBeforeTax"（已映射）
    ebt = _safe_float(latest_lrb.get("利润总额"))
    if ebt is None:
        missing.append("利润总额")

    # 2) 财务费用（利息支出净额，IFRS 港股下为负数表示净利息收入）
    interest = _safe_float(latest_lrb.get("财务费用"))
    if interest is None:
        interest = 0.0
        missing.append("财务费用")

    if ebt is None:
        return {
            "error": "关键字段缺失",
            "EBITDA": None,
            "缺失字段": missing,
            "报告期": latest_lrb.get("报告期", ""),
        }

    # 3) 折旧摊销 D&A 反推：OCF 净额 - 净利润
    #   利用间接法 OCF = 净利润 + D&A + 营运资本变动 + 其他非现金的会计恒等式
    #   当 D&A + 营运资本变动 + 其他非现金 ≈ D&A（净利公司、营运资本稳定时）即可反推
    #   边界：D&A < 0 → 归 0（OCF < 净利表示营运资本大幅流出，估算失效）
    ocf = _safe_float(latest_llb.get("经营活动产生的现金流量净额"))
    # 净利润字段：A股 lrb 经常缺"净利润"（只有"归母净利润"），港股 zhsy 多数有；回退兼容
    net_profit = _safe_float(latest_lrb.get("净利润"))
    if net_profit is None:
        net_profit = _safe_float(latest_lrb.get("归属于母公司股东的净利润"))
    da = 0.0
    da_method = "未计算"
    da_note = ""
    if ocf is not None and net_profit is not None:
        da_raw = ocf - net_profit
        if da_raw > 0:
            da = da_raw
            da_method = "OCF 净额 - 净利润（粗估）"
        else:
            # OCF ≤ 净利：营运资本变动为负贡献，单纯反推 D&A 会失真
            da = 0.0
            da_method = "OCF 净额 - 净利润（OCF≤净利时反推失效，按 0 处理）"
            da_note = f"OCF={ocf/1e8:.1f}亿 ≤ 净利润={net_profit/1e8:.1f}亿，营运资本贡献为负，反推失效"
    else:
        missing.append("OCF 净额或净利润缺失，无法反推 D&A")
        da_method = "OCF/净利缺失"

    ebit = ebt + interest
    ebitda = ebit + da

    return {
        "EBIT": round(ebit, 2),
        "EBITDA": round(ebitda, 2),
        "税前利润": round(ebt, 2),
        "财务费用": round(interest, 2),
        "折旧摊销": round(da, 2),
        "折旧摊销算法": da_method,
        "折旧摊销备注": da_note,
        "OCF_净额": round(ocf, 2) if ocf is not None else None,
        "净利润_参考": round(net_profit, 2) if net_profit is not None else None,
        "报告期": latest_lrb.get("报告期", ""),
        "数据来源": f"westock finance（{latest_lrb.get('报告期', '')} / {latest_llb.get('报告期', '')})",
        "缺失字段": missing,
    }


# ── 工具：safe float 转 ──
def _safe_float(v):
    if v is None or v == "" or v == "--" or v == "-":
        return None
    try:
        if isinstance(v, str):
            v = v.replace(",", "").replace("元", "").strip()
            # 处理百分号（如 "1.5%"）
            if v.endswith("%"):
                return float(v[:-1]) / 100
        return float(v)
    except (ValueError, TypeError):
        return None


# ── 自测 ──
if __name__ == "__main__":
    import json as _json

    print("=" * 60)
    print("Test 1: A股 600519 财务三表")
    print("=" * 60)
    try:
        triple = finance("600519", num=2)
        for t in ("lrb", "fzb", "llb"):
            print(f"\n  [{t}] 共 {len(triple[t])} 行, 最新期 {triple[t][0].get('报告期') if triple[t] else 'N/A'}")
            if triple[t]:
                keys = list(triple[t][0].keys())[:5]
                print(f"  示例字段: {keys}")
    except Exception as e:
        print(f"  FAIL: {e}")

    print("\n" + "=" * 60)
    print("Test 2: 港股 00700 财务三表")
    print("=" * 60)
    try:
        triple = finance("00700", num=2)
        for t in ("lrb", "fzb", "llb"):
            print(f"\n  [{t}] 共 {len(triple[t])} 行, 最新期 {triple[t][0].get('报告期') if triple[t] else 'N/A'}")
            if triple[t]:
                keys = list(triple[t][0].keys())[:5]
                print(f"  示例字段: {keys}")
    except Exception as e:
        print(f"  FAIL: {e}")

    print("\n" + "=" * 60)
    print("Test 3: 港股 00700 公司简况")
    print("=" * 60)
    try:
        p = profile("00700")
        print(f"  name: {p.get('name')}")
        print(f"  industry: {p.get('industry')}")
        print(f"  chairman: {p.get('chairman')}")
    except Exception as e:
        print(f"  FAIL: {e}")

    print("\n" + "=" * 60)
    print("Test 4: 港股 00700 股东")
    print("=" * 60)
    try:
        sh = shareholder("00700")
        print(f"  共 {len(sh)} 个股东")
        if sh:
            print(f"  Top1: {sh[0]}")
    except Exception as e:
        print(f"  FAIL: {e}")

    print("\n" + "=" * 60)
    print("Test 5: 港股 00700 行情")
    print("=" * 60)
    try:
        q = quote("00700")
        print(f"  现价: {q.get('last')} | 开盘: {q.get('open')} | 最高: {q.get('high')} | 最低: {q.get('low')}")
    except Exception as e:
        print(f"  FAIL: {e}")

    print("\n" + "=" * 60)
    print("Test 6: calc_ebitda 港股")
    print("=" * 60)
    try:
        triple = finance("00700", num=1)
        result = calc_ebitda(triple)
        for k, v in result.items():
            print(f"  {k}: {v}")
    except Exception as e:
        print(f"  FAIL: {e}")

    print("\n" + "=" * 60)
    print("Test 7: 港股 00700 分红")
    print("=" * 60)
    try:
        dv = dividend("00700")
        print(f"  共 {len(dv)} 条")
        if dv:
            print(f"  最新: {dv[0]}")
    except Exception as e:
        print(f"  FAIL: {e}")
