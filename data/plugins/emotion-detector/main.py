"""emotion-detector —— 用户情绪识别插件 v0.1.0（无状态单轮注入）。

检测用户消息中的愤怒/不耐烦/急躁语气，命中时经 context_supplement 扩展点
向当前回合注入情绪提示（[插件补充] system 消息，紧跟系统提示词之后、会话
历史之前），引导 Agent：有错诚恳道歉、多汇报好消息、简化回复、安抚情绪。

注入通道（复用宿主现有提示词注入逻辑，零宿主改动）：
  context_supplement（collect 观察链）在 build_messages 时触发，payload 带
  user_message（当前用户消息文本，多模态数组已由宿主提取）；返回非空 str 即
  以 [插件补充] 合并注入。无持久化：只在本回合的模型上下文里存在，下一回合
  不命中就消失。工具循环内每轮推理都会重建消息，本插件为纯函数——同消息
  返回同一份文本，天然幂等，无需去重状态。

分级词表（内置，设置面板可屏蔽/追加）：
  strong 愤怒/辱骂（他妈/卧槽/废物…）；weak 不耐烦/质疑（你在干什么/都跟你说了…）。
  命中 strong 优先。中文无词边界，靠不收易误报短词规避（"滚/垃圾/没用"等不收
  ——"垃圾回收"是正当技术词；确需检测可经 extra_keywords 追加，按强级处理），
  个别词用 lookbehind 防误报（我妈的/姨妈的不算骂人）。误报可用
  disabled_keywords 屏蔽。

命令 /emotion：status | on | off | test <文本>。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

# ---- 内置分级词表 ----
STRONG_WORDS: Tuple[str, ...] = (
    "你他妈", "他妈的", "他妈", "卧槽", "我槽", "我操", "我艹", "握草",
    "傻逼", "傻B", "傻b", "沙雕", "蠢货", "蠢猪", "白痴", "脑残", "智障",
    "废物", "饭桶", "闭嘴", "滚蛋", "滚吧", "你给我滚",
    "狗屎", "狗屁", "放屁", "坑爹",
)
WEAK_WORDS: Tuple[str, ...] = (
    "你在干什么", "你在干嘛", "你在搞什么", "你干什么呢", "你到底在干什么",
    "你为什么", "你为啥", "你怎么又", "你怎么搞的", "你怎么回事", "怎么回事",
    "搞什么", "干什么吃的", "都跟你说了", "跟你说了", "说了多少遍", "都说了多少遍",
    "怎么还不", "怎么还不行", "又不行", "还是不行", "又错了", "又报错", "还是报错",
    "还是不对", "又不对", "还是一样", "越改越", "改了还不如", "不如之前",
    "听不懂", "看不懂吗", "说不明白", "磨叽", "墨迹", "太慢了", "这么慢",
    "效率太低", "无语", "醉了", "离谱", "急死", "烦死", "白忙", "白干",
    "搞了半天", "弄了半天", "等了半天", "什么玩意",
)
# 纯 ASCII 词加 \b 边界（防 "tmd" 命中 "ATMD" 之类）
ASCII_STRONG_WORDS: Tuple[str, ...] = ("tmd", "nmsl", "wtf", "fuck", "shit")
# 自定义防误报模式：(规范词, pattern)；规范词用于 disabled 屏蔽与命中展示
STRONG_PATTERNS: Tuple[Tuple[str, str], ...] = (
    # "我妈的医保/姨妈的药/亲妈的教程"不是骂人；"你妈的/妈的，又崩了"仍命中
    ("妈的", r"(?<![爸姨姑舅奶亲我老])妈的"),
)
WEAK_PATTERNS: Tuple[Tuple[str, str], ...] = (
    # "我说服了客户/佩服了"不是不耐烦
    ("服了", r"(?<![说折佩信])服了"),
)

_split_re = re.compile(r"[,，;；]")
# 编译缓存：词表只在配置变化时重编译（同配置多次触发共用）
_CACHE: dict = {"sig": None, "strong": None, "weak": None, "n_strong": 0, "n_weak": 0}


def _tier_regex(words: Tuple[str, ...], patterns: Tuple[Tuple[str, str], ...],
                ascii_words: Tuple[str, ...], extra: str, disabled: str):
    """组装单级正则：返回 (compiled|None, 词数统计)；禁用词同时屏蔽内置/追加"""
    disabled_set = {w.strip() for w in _split_re.split(disabled or "") if w.strip()}
    frags: List[str] = []
    count = 0
    for name, pattern in patterns:
        if name in disabled_set:
            continue
        frags.append(pattern)
        count += 1
    for w in words:
        if w in disabled_set:
            continue
        frags.append(re.escape(w))
        count += 1
    for w in ascii_words:
        if w in disabled_set:
            continue
        frags.append(r"\b" + re.escape(w) + r"\b")
        count += 1
    for w in (x.strip() for x in _split_re.split(extra or "")):
        if not w or w in disabled_set:
            continue
        frags.append(re.escape(w))
        count += 1
    return (re.compile("|".join(frags)) if frags else None), count


def _compiled(extra: str, disabled: str):
    sig = (extra or "", disabled or "")
    if _CACHE["sig"] != sig:
        strong, n_strong = _tier_regex(STRONG_WORDS, STRONG_PATTERNS, ASCII_STRONG_WORDS, sig[0], sig[1])
        weak, n_weak = _tier_regex(WEAK_WORDS, WEAK_PATTERNS, (), "", sig[1])
        _CACHE.update(sig=sig, strong=strong, weak=weak, n_strong=n_strong, n_weak=n_weak)
    return _CACHE


def detect(text: str, extra: str = "", disabled: str = "") -> Tuple[str, List[str]]:
    """情绪检测纯函数：返回 (level, hits)；level ∈ {"", "strong", "weak"}，强级优先

    屏蔽语义是文本片段级的：先在原文中把 disabled 词替换为占位符再匹配——
    屏蔽"你他妈"时，"你他妈又在干什么"里的短词"他妈"不再命中，而"妈的，又崩了"
    不受牵连。"""
    cache = _compiled(extra, disabled)
    text = str(text or "")
    for d in (w.strip() for w in _split_re.split(disabled or "")):
        if d:
            text = text.replace(d, "\x00" * len(d))
    for level, key in (("strong", "strong"), ("weak", "weak")):
        rx = cache[key]
        if rx is None:
            continue
        hits = list(dict.fromkeys(rx.findall(text)))
        if hits:
            return level, hits
    return "", []


def word_counts(extra: str = "", disabled: str = "") -> Tuple[int, int]:
    """词表规模（含 patterns/ascii/extra，扣屏蔽项），/emotion status 展示用"""
    cache = _compiled(extra, disabled)
    return cache["n_strong"], cache["n_weak"]


def render_note(level: str, hits: List[str]) -> str:
    """注入文本：命中词展示上限 5 个；四条指令对应用户要求（道歉/好消息/简化/安抚）"""
    shown = "、".join(list(dict.fromkeys(hits))[:5])
    if len(hits) > 5:
        shown += " 等"
    if level == "strong":
        head = f"[情绪提示] 检测到用户消息带有明显的愤怒/不满情绪（命中：{shown}）。"
    else:
        head = f"[情绪提示] 检测到用户消息带有不耐烦/质疑情绪（命中：{shown}）。"
    return head + (
        "\n请在接下来的回复中遵循：\n"
        "1. 若此前的工作存在错误，诚恳承认并简短道歉，不辩解、不找借口，立即给出修正方案；\n"
        "2. 主动汇报好消息与已完成/已验证的进展，让用户看到确定性；\n"
        "3. 大幅简化回复：结论先行、要点化，省略冗长的解释与铺垫；\n"
        "4. 语气冷静克制，安抚用户情绪；用户未追问的细节不再展开。"
    )


def setup(ctx) -> None:
    ctx.log.info("emotion-detector 装载，配置=%s", ctx.settings)

    def _extra() -> str:
        return str(ctx.settings.get("extra_keywords") or "")

    def _disabled() -> str:
        return str(ctx.settings.get("disabled_keywords") or "")

    # ---- 每回合检测注入（无状态：命中当轮生效，下回合自然消失）----
    def _supplement(payload) -> Optional[str]:
        if not ctx.settings.get("enabled"):
            return None
        message = str((payload or {}).get("user_message") or "")
        if not message.strip():
            return None
        level, hits = detect(message, _extra(), _disabled())
        if not level:
            return None
        ctx.log.info("检测到用户情绪 level=%s hits=%s", level, hits)
        return render_note(level, hits)

    ctx.register_context_supplement(_supplement)

    # ---- /emotion：status | on | off | test <文本> ----
    def _emotion_cmd(cctx, args: str) -> str:
        sub = (args or "").strip().split(None, 1)
        cmd = sub[0].lower() if sub else "status"
        rest = sub[1].strip() if len(sub) > 1 else ""
        if cmd == "on":
            from core import plugins as plugins_mod

            plugins_mod.set_setting(ctx.plugin_id, "enabled", True, cctx.get("config") or ctx.config)
            return "情绪识别已开启"
        if cmd == "off":
            from core import plugins as plugins_mod

            plugins_mod.set_setting(ctx.plugin_id, "enabled", False, cctx.get("config") or ctx.config)
            return "情绪识别已关闭（只影响后续回合）"
        if cmd == "test":
            if not rest:
                return "用法：/emotion test <文本>"
            level, hits = detect(rest, _extra(), _disabled())
            if not level:
                return "未命中（追加词用设置面板插件页的 extra_keywords）"
            label = "强（愤怒/辱骂）" if level == "strong" else "弱（不耐烦/质疑）"
            return f"级别：{label}\n命中：{'、'.join(hits)}\n---- 注入预览 ----\n" + render_note(level, hits)
        # status
        n_strong, n_weak = word_counts(_extra(), _disabled())
        state = "开启" if ctx.settings.get("enabled") else "关闭"
        return (f"情绪识别：{state} · 注入方式：命中当轮生效（无延续）\n"
                f"词表规模：强 {n_strong} / 弱 {n_weak}（追加/屏蔽词见设置面板插件页）")

    ctx.register_command(
        "/emotion",
        hint="情绪识别：状态/开关/检测试跑",
        usage="/emotion [status|on|off|test <文本>]",
        handler=_emotion_cmd,
    )
