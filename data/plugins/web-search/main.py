"""web-search —— 必应网络搜索插件 v0.1.0（无状态单次请求）。

注册模型可见工具 web_search：关键词 → 拼接必应搜索 URL → 以真实浏览器头
请求结果页 → BeautifulSoup 解析为结构化结果（标题/链接/摘要）返回给模型。

- 请求头取值与 core webfetch 保持一致，另按配置的必应地址动态补 Referer，
  模拟从必应首页发起搜索的真实用户
- 必应结果链接经 bing.com/ck/a?...&u=a1<base64url> 重定向包裹，解析时解码
  还原真实 URL，解码失败保留原链接
- 解析到 0 条时返回页面标题与诊断提示而非空结果，避免模型空转重试
- 配置（max_results / base_url）经 ctx.settings 每次调用实时读取，改设置即生效
"""
from __future__ import annotations

from bawcode_sdk import PluginContext

import base64
import binascii
import re
from typing import List, Tuple
from urllib.parse import quote

# 真实浏览器请求头，取值与 core webfetch 的 _WEBFETCH_HEADERS 一致；Referer 动态补
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

MAX_BYTES = 5 * 1024 * 1024  # 响应体上限，防超大页面
MAX_CHARS = 20000            # 返回文本硬顶，防撑爆上下文
DEFAULT_TIMEOUT = 20

# ck/a 重定向里 base64url 编码的真实 URL（u=a1 前缀）；兼容以 ~ . 补位的变体
CK_URL_RE = re.compile(r"[?&]u=a1([A-Za-z0-9_~.-]+)")


def load_deps():
    """
    懒加载 requests 与 BeautifulSoup；缺失返回 None 由调用方给依赖提示
    """
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    return requests, BeautifulSoup


def decode_bing_url(href: str) -> str:
    """
    还原必应 ck/a 重定向链接包裹的真实 URL；非重定向或解码失败原样返回
    """
    if "/ck/a" not in href:
        return href
    m = CK_URL_RE.search(href)
    if not m:
        return href
    payload = m.group(1)
    payload += "=" * (-len(payload) % 4)
    try:
        text = base64.urlsafe_b64decode(payload).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return href
    return text if text.startswith(("http://", "https://")) else href


def parse_results(soup, limit: int) -> List[Tuple[str, str, str]]:
    """
    解析必应结果页：li.b_algo → (标题, 链接, 摘要)，最多 limit 条
    """
    results: List[Tuple[str, str, str]] = []
    for item in soup.select("li.b_algo"):
        a = item.select_one("h2 a") or item.select_one("a")
        if a is None or not a.get("href"):
            continue
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        link = decode_bing_url(a["href"])
        p = item.select_one(".b_caption p") or item.find("p")
        snippet = p.get_text(" ", strip=True) if p else ""
        results.append((title, link, snippet))
        if len(results) >= limit:
            break
    return results


def setup(ctx: PluginContext):
    @ctx.register_tool(
        name="web_search",
        description=(
            "Search the web via Microsoft Bing and return structured results "
            "(numbered list of title/link/snippet). Use when the task needs "
            "up-to-date information beyond the codebase and local files; "
            "combine with webfetch to read a specific result page"
        ),
        usage="web_search <query> [count] [timeout]",
        schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords",
                },
                "count": {
                    "type": "integer",
                    "description": "Max results, optional, default 10 (plugin settings), clamped to 1-30",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Request timeout in seconds, optional, default 20, clamped to 1-60",
                },
            },
            "required": ["query"],
        },
    )
    def web_search(query: str, count: int = 0, timeout: int = DEFAULT_TIMEOUT) -> str:
        """网络搜索：关键词 → 必应结果页 → 结构化结果（标题/链接/摘要）"""
        deps = load_deps()
        if deps is None:
            return "web_search 依赖缺失（requests/beautifulsoup4/lxml），请安装插件目录 requirements.txt"
        requests, BeautifulSoup = deps

        query = str(query or "").strip()
        if not query:
            return "query 不能为空：请提供搜索关键词"

        settings = ctx.settings
        base_url = str(settings.get("base_url") or "https://www.bing.com").rstrip("/")
        try:
            limit = int(count) if count else int(settings.get("max_results") or 10)
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 30))
        try:
            seconds = int(timeout)
        except (TypeError, ValueError):
            seconds = DEFAULT_TIMEOUT
        seconds = max(1, min(seconds, 60))

        url = f"{base_url}/search?q={quote(query)}&count={limit}&mkt=zh-CN"
        headers = dict(BROWSER_HEADERS)
        headers["Referer"] = f"{base_url}/"

        ctx.log.info("web_search: %s（count=%d timeout=%ds）", query, limit, seconds)
        try:
            with requests.get(
                url,
                headers=headers,
                timeout=seconds,
                allow_redirects=True,
                stream=True,
            ) as resp:
                resp.raise_for_status()
                chunks = []
                total = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > MAX_BYTES:
                        ctx.log.warn("web_search 响应超限截断: %s（>%d bytes）", query, MAX_BYTES)
                        break
                content = b"".join(chunks)
        except requests.exceptions.Timeout:
            return f"搜索超时（>{seconds}s）: {query}"
        except requests.exceptions.SSLError as e:
            return f"SSL 错误: {e}"
        except requests.exceptions.RequestException as e:
            ctx.log.warn("web_search 失败: %s（%s）", query, e)
            return f"搜索失败: {e}"

        soup = BeautifulSoup(content, "lxml")
        results = parse_results(soup, limit)
        if not results:
            # 0 条多为风控页/结构变化：带页面标题与建议返回，不留空输出
            title = soup.title.get_text(strip=True) if soup.title else ""
            return (
                "未解析到搜索结果（疑似必应风控或页面结构变化）。\n"
                f"页面标题: {title or '无'}\n"
                "建议：稍后重试，或经设置面板把 base_url 换成 https://cn.bing.com"
            )

        lines = [f"搜索「{query}」共 {len(results)} 条结果（{base_url}）", ""]
        for i, (title, link, snippet) in enumerate(results, 1):
            lines.append(f"{i}. [{title}]({link})")
            if snippet:
                lines.append(f"   {snippet}")
            lines.append("")
        text = "\n".join(lines).strip()
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + f"\n\n[内容已截断：原始 {len(text)} 字符，上限 {MAX_CHARS}]"
        ctx.log.debug("web_search 完成: %s（%d 条）", query, len(results))
        return text
