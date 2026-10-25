#!/usr/bin/env python3
"""
Trapped-ion arXiv watcher for Telegram.

Modes
  python bot.py run       long-running bot: answers commands, checks arXiv every N hours
  python bot.py once      one check, post new papers, exit (cron / GitHub Actions)
  python bot.py dry-run   one check, print results to the terminal (no Telegram, no state)
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ARXIV_API = "https://export.arxiv.org/api/query"
USER_AGENT = "trapped-ion-arxiv-telegram-bot/1.0"
TG_LIMIT = 4096
SEEN_TTL_DAYS = 120

log = logging.getLogger("ion-bot")


# ───────────────────────────── LaTeX cleanup ─────────────────────────────

_SUP_SRC, _SUP_DST = "0123456789+-−()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁽⁾ⁿ"
_SUB_SRC, _SUB_DST = "0123456789+-()", "₀₁₂₃₄₅₆₇₈₉₊₋₍₎"
_SUP = str.maketrans(_SUP_SRC, _SUP_DST)
_SUB = str.maketrans(_SUB_SRC, _SUB_DST)
_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "Delta": "Δ", "epsilon": "ε",
    "eta": "η", "theta": "θ", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "pi": "π",
    "rho": "ρ", "sigma": "σ", "tau": "τ", "phi": "φ", "Phi": "Φ", "chi": "χ", "psi": "ψ",
    "omega": "ω", "Omega": "Ω", "hbar": "ħ", "times": "×", "approx": "≈", "sim": "~",
    "leq": "≤", "geq": "≥", "pm": "±", "cdot": "·", "rightarrow": "→", "to": "→",
}


def detex(s: str) -> str:
    """Turn common arXiv LaTeX ($^{40}$Ca$^+$, \\mathrm{Yb}^+) into readable Unicode."""
    s = re.sub(r"\s+", " ", s or "").strip()
    s = re.sub(r"\{\\(?:rm|it|bf)\s+([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\(?:mathrm|text|textrm|mathit|mathbf|rm|operatorname)\{([^{}]*)\}", r"\1", s)

    def script(m: re.Match, table, allowed: str, mark: str) -> str:
        x = m.group(1) if m.group(1) is not None else m.group(2)
        x = x.replace("$", "")
        return x.translate(table) if x and all(c in allowed for c in x) else f"{mark}{x}"

    s = re.sub(r"\^\{([^{}]*)\}|\^\$?([^\s{$])", lambda m: script(m, _SUP, _SUP_SRC, "^"), s)
    s = re.sub(r"_\{([^{}]*)\}|_\$?([^\s{$])", lambda m: script(m, _SUB, _SUB_SRC, "_"), s)
    s = re.sub(r"\\([A-Za-z]+)", lambda m: _GREEK.get(m.group(1), m.group(1)), s)
    s = s.replace("$", "").replace("\\%", "%").replace("~", " ")
    s = re.sub(r"(?<=\w)\{([^{}]*)\}", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


# ───────────────────────────── arXiv ─────────────────────────────

@dataclass
class Paper:
    id: str            # 2410.12345 (no version)
    version: str       # 2410.12345v2
    title: str
    summary: str
    authors: list[str]
    published: datetime
    updated: datetime
    categories: list[str]
    primary: str
    comment: str = ""

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.summary}"

    @property
    def abs_url(self) -> str:
        return f"https://arxiv.org/abs/{self.id}"

    @property
    def pdf_url(self) -> str:
        return f"https://arxiv.org/pdf/{self.id}"


NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def _dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))


class ArxivClient:
    MIN_INTERVAL = 3.1  # arXiv asks for ≥3 s between API calls

    def __init__(self, cfg: dict):
        a = cfg["arxiv"]
        self.categories = a["categories"]
        self.terms = a["query_terms"]
        self.max_results = int(a.get("max_results", 200))
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._lock = threading.Lock()
        self._last = 0.0

    @staticmethod
    def term(t: str, field: str = "all") -> str:
        t = re.sub(r"[^\w\s\-+.]", "", t).strip()
        return f'{field}:"{t}"' if re.search(r"[\s\-]", t) else f"{field}:{t}"

    def base_query(self) -> str:
        cats = " OR ".join(f"cat:{c}" for c in self.categories)
        terms = " OR ".join(self.term(t) for t in self.terms)
        return f"({cats}) AND ({terms})"

    def search(self, extra: str | None = None, max_results: int | None = None) -> list[Paper]:
        q = self.base_query()
        if extra:
            q = f"{q} AND ({extra})"
        params = {
            "search_query": q,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "start": 0,
            "max_results": max_results or self.max_results,
        }
        with self._lock:
            wait = self.MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                text = self._get(params)
            finally:
                self._last = time.monotonic()
        return self.parse(text)

    def _get(self, params: dict) -> str:
        err = None
        for attempt in range(4):
            try:
                r = self.session.get(ARXIV_API, params=params, timeout=90)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r.text
            except requests.RequestException as e:
                err = e
                delay = 10 * (attempt + 1)
                log.warning("arXiv request failed (%s), retrying in %ss", e, delay)
                time.sleep(delay)
        raise RuntimeError(f"arXiv API unavailable: {err}")

    @staticmethod
    def parse(text: str) -> list[Paper]:
        root = ET.fromstring(text)
        papers = []
        for e in root.findall("a:entry", NS):
            get = lambda tag: (e.findtext(tag, default="", namespaces=NS) or "").strip()
            eid = get("a:id")
            if "/api/errors" in eid:
                raise ValueError(f"arXiv rejected the query: {get('a:summary')}")
            version = eid.rsplit("/abs/", 1)[-1]
            if not version:
                continue
            prim = e.find("arxiv:primary_category", NS)
            papers.append(Paper(
                id=re.sub(r"v\d+$", "", version),
                version=version,
                title=detex(get("a:title")),
                summary=detex(get("a:summary")),
                authors=[(a.findtext("a:name", default="", namespaces=NS) or "").strip()
                         for a in e.findall("a:author", NS)],
                published=_dt(get("a:published")),
                updated=_dt(get("a:updated")),
                categories=[c.get("term") for c in e.findall("a:category", NS) if c.get("term")],
                primary=prim.get("term", "") if prim is not None else "",
                comment=detex(get("arxiv:comment")),
            ))
        return papers


# ───────────────────────────── classification ─────────────────────────────

def compile_terms(terms) -> list[re.Pattern]:
    out = []
    for t in terms or []:
        t = str(t)
        if t.startswith("re:"):
            out.append(re.compile(t[3:], re.I))
        else:
            parts = [re.escape(p) for p in re.split(r"[\s\-–]+", t.strip()) if p]
            out.append(re.compile(r"(?<![A-Za-z])" + r"[\s\-–]+".join(parts), re.I))
    return out


class Classifier:
    def __init__(self, cfg: dict):
        rel = cfg["relevance"]
        self.require = compile_terms(rel["require_any"])
        self.exclude = compile_terms(rel.get("exclude_any"))
        self.experiment = compile_terms(rel.get("experiment_markers"))
        self.topics = [(k, v["label"], compile_terms(v["keywords"])) for k, v in cfg["topics"].items()]
        self.other_label = cfg.get("other_topic_label", "🔬 Other")

    @staticmethod
    def _any(pats, text) -> bool:
        return any(p.search(text) for p in pats)

    def relevant(self, p: Paper) -> bool:
        return self._any(self.require, p.text) and not self._any(self.exclude, p.text)

    def topic_keys(self, p: Paper) -> list[str]:
        return [k for k, _, pats in self.topics if self._any(pats, p.text)]

    def labels(self, p: Paper) -> list[str]:
        keys = set(self.topic_keys(p))
        labels = [label for k, label, _ in self.topics if k in keys]
        return labels or [self.other_label]

    def is_experiment(self, p: Paper) -> bool:
        return self._any(self.experiment, p.summary)


# ───────────────────────────── formatting ─────────────────────────────

def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def shorten(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


def author_line(p: Paper, n: int = 5) -> str:
    a = ", ".join(p.authors[:n])
    if len(p.authors) > n:
        a += f" + {len(p.authors) - n} more"
    return a


def format_paper(p: Paper, cls: Classifier, abstract_chars: int = 600) -> str:
    tags = cls.labels(p) + (["🧪 Experiment"] if cls.is_experiment(p) else [])
    lines = [
        f"<b>{esc(p.title)}</b>",
        f"<i>{esc(author_line(p))}</i>",
        esc(" · ".join(tags)),
    ]
    if abstract_chars > 0:
        lines += ["", esc(shorten(p.summary, min(abstract_chars, 3000)))]
    lines += ["", f'<a href="{p.abs_url}">arXiv:{p.id}</a> · <a href="{p.pdf_url}">PDF</a> · '
                  f"{esc(p.primary)} · {p.published:%d %b %Y}"]
    return "\n".join(lines)


def format_header(papers: list[Paper], cls: Classifier) -> str:
    n = len(papers)
    counts: dict[str, int] = {}
    for p in papers:
        for label in cls.labels(p):
            counts[label] = counts.get(label, 0) + 1
    exp = sum(cls.is_experiment(p) for p in papers)
    lines = [f"⚛️ <b>{n} new trapped-ion paper{'s' if n != 1 else ''} on arXiv</b>"]
    lines += [f"{esc(k)}: {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    if exp:
        lines.append(f"🧪 Experimental: {exp}")
    return "\n".join(lines)


def format_compact(papers: list[Paper], cls: Classifier) -> list[str]:
    groups: dict[str, list[Paper]] = {}
    for p in papers:
        groups.setdefault(cls.labels(p)[0], []).append(p)
    lines = [format_header(papers, cls), ""]
    for label, ps in groups.items():
        lines.append(f"<b>{esc(label)}</b>")
        for p in ps:
            first = p.authors[0] + (" et al." if len(p.authors) > 1 else "") if p.authors else ""
            flag = " 🧪" if cls.is_experiment(p) else ""
            lines.append(f'• <a href="{p.abs_url}">{esc(p.title)}</a> — {esc(first)}{flag}')
        lines.append("")
    return chunk_lines(lines)


def chunk_lines(lines: list[str], limit: int = 4000) -> list[str]:
    chunks, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur.rstrip())
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur.rstrip())
    return chunks


def to_plain(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s))


# ───────────────────────────── state ─────────────────────────────

class State:
    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        data = {}
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
        self.seen: dict[str, str] = data.get("seen", {})
        self.subscribers: list[int] = data.get("subscribers", [])
        self.last_check: str | None = data.get("last_check")

    def mark_seen(self, ids, when: datetime):
        with self.lock:
            for i in ids:
                self.seen.setdefault(i, when.isoformat())
            self.last_check = when.isoformat()

    def save(self):
        if not self.path:
            return
        with self.lock:
            cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_TTL_DAYS)
            self.seen = {k: v for k, v in self.seen.items() if datetime.fromisoformat(v) > cutoff}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "last_check": self.last_check,
                "subscribers": self.subscribers,
                "seen": self.seen,
            }, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)


def collect_new(arxiv: ArxivClient, cls: Classifier, state: State, cfg: dict):
    """Return (new relevant papers oldest-first, ids of everything fetched, timestamp)."""
    sched = cfg["schedule"]
    now = datetime.now(timezone.utc)
    first = state.last_check is None
    max_age = timedelta(days=float(sched.get("first_run_lookback_days", 3) if first
                                   else sched.get("max_age_days", 14)))
    papers = arxiv.search()
    fresh = []
    with state.lock:
        for p in papers:
            if p.id in state.seen or now - p.published > max_age:
                continue
            if cls.relevant(p):
                fresh.append(p)
    fresh.sort(key=lambda p: p.published)
    log.info("fetched %d papers from arXiv, %d new and relevant", len(papers), len(fresh))
    return fresh, [p.id for p in papers], now


# ───────────────────────────── Telegram ─────────────────────────────

class TelegramError(Exception):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

    def call(self, method: str, **params):
        timeout = params.get("timeout", 0) + 30
        for attempt in range(5):
            try:
                r = self.session.post(f"{self.base}/{method}", json=params, timeout=timeout)
                data = r.json()
            except (requests.RequestException, ValueError) as e:
                log.warning("Telegram %s failed (%s)", method, e)
                time.sleep(3 * (attempt + 1))
                continue
            if data.get("ok"):
                return data["result"]
            if data.get("error_code") == 429:
                time.sleep(data.get("parameters", {}).get("retry_after", 5) + 1)
                continue
            raise TelegramError(data.get("description", "unknown error"), data.get("error_code"))
        raise TelegramError(f"Telegram unreachable for {method}")

    def send(self, chat_id: int, text: str):
        return self.call("sendMessage", chat_id=chat_id, text=text[:TG_LIMIT], parse_mode="HTML",
                         link_preview_options={"is_disabled": True})


def env_chat_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    return {int(x) for x in re.split(r"[,\s]+", raw) if x.strip().lstrip("-").isdigit()}


# ───────────────────────────── bot ─────────────────────────────

HELP = """⚛️ <b>Trapped-ion arXiv bot</b>
I watch arXiv for new trapped-ion papers — quantum computing, simulation, clocks, networks &amp; memories, sensing, hardware, molecular/HCI ions — and post them here.

/subscribe — get new papers in this chat
/unsubscribe — stop
/latest [n] — newest n relevant papers (default 5)
/topic &lt;key&gt; [n] — newest papers in one topic
/topics — list topic keys
/search &lt;words&gt; — search within trapped-ion papers, e.g. <code>/search "nuclear clock" thorium</code>
/check — check for new papers now
/status — bot status"""

COMMANDS = [
    ("subscribe", "Get new papers in this chat"),
    ("unsubscribe", "Stop updates"),
    ("latest", "Newest papers: /latest 5"),
    ("topic", "Newest in a topic: /topic clocks"),
    ("topics", "List topics"),
    ("search", "Search trapped-ion papers"),
    ("check", "Check arXiv now"),
    ("status", "Bot status"),
    ("help", "Help"),
]


class Bot:
    def __init__(self, cfg, state: State, arxiv: ArxivClient, cls: Classifier, tg: Telegram):
        self.cfg, self.state, self.arxiv, self.cls, self.tg = cfg, state, arxiv, cls, tg
        t = cfg["telegram"]
        self.allowed = {int(x) for x in t.get("allowed_user_ids") or []}
        self.default_chats = {int(x) for x in t.get("default_chat_ids") or []} | env_chat_ids()
        self.mode = t.get("mode", "individual")
        self.abstract_chars = int(t.get("abstract_chars", 600))
        self.interval_h = float(cfg["schedule"].get("check_every_hours", 3))
        self.check_lock = threading.Lock()

    # ---- delivery ----
    def chats(self) -> set[int]:
        return set(self.state.subscribers) | self.default_chats

    def send_papers(self, chat_id: int, papers: list[Paper], header: bool = True):
        if self.mode == "compact":
            messages = format_compact(papers, self.cls)
        else:
            messages = ([format_header(papers, self.cls)] if header else []) + \
                       [format_paper(p, self.cls, self.abstract_chars) for p in papers]
        pause = 3.1 if chat_id < 0 else 1.1  # Telegram limits groups to ~20 msg/min
        for m in messages:
            self.tg.send(chat_id, m)
            time.sleep(pause)

    def check_and_deliver(self) -> int:
        with self.check_lock:
            chats = self.chats()
            if not chats:
                log.info("no subscribers yet — skipping check")
                return 0
            fresh, ids, now = collect_new(self.arxiv, self.cls, self.state, self.cfg)
            for chat in chats:
                if not fresh:
                    break
                try:
                    self.send_papers(chat, fresh)
                except TelegramError as e:
                    log.error("could not deliver to %s: %s", chat, e)
                    if e.code in (400, 403) and chat in self.state.subscribers:
                        with self.state.lock:
                            self.state.subscribers.remove(chat)
            self.state.mark_seen(ids, now)
            self.state.save()
            return len(fresh)

    # ---- commands ----
    def reply(self, chat_id: int, text: str):
        try:
            self.tg.send(chat_id, text)
        except TelegramError as e:
            log.error("reply to %s failed: %s", chat_id, e)

    def handle(self, update: dict):
        msg = update.get("message") or update.get("channel_post")
        if not msg or not msg.get("text", "").startswith("/"):
            return
        chat_id = msg["chat"]["id"]
        user_id = (msg.get("from") or {}).get("id")
        cmd, _, arg = msg["text"].strip().partition(" ")
        cmd = cmd[1:].split("@")[0].lower()
        arg = arg.strip()

        if self.allowed and user_id not in self.allowed:
            if cmd in ("start", "help"):
                self.reply(chat_id, f"Sorry, this bot is private. Your user id is <code>{user_id}</code>.")
            return

        handler = getattr(self, f"cmd_{cmd}", None)
        if handler is None:
            return
        try:
            handler(chat_id, arg)
        except ValueError as e:
            self.reply(chat_id, f"⚠️ {esc(str(e))}")
        except Exception as e:  # keep the bot alive whatever happens
            log.exception("command /%s failed", cmd)
            self.reply(chat_id, f"⚠️ Something went wrong: {esc(str(e))}")

    def cmd_start(self, chat_id, arg):
        self.reply(chat_id, HELP + f"\n\nThis chat's id: <code>{chat_id}</code>")

    cmd_help = cmd_start

    def cmd_subscribe(self, chat_id, arg):
        with self.state.lock:
            if chat_id not in self.state.subscribers:
                self.state.subscribers.append(chat_id)
            self.state.save()
        self.reply(chat_id, f"✅ Subscribed. I check arXiv every {self.interval_h:g} h and post new "
                            f"trapped-ion papers here. Try /latest to see recent ones.")

    def cmd_unsubscribe(self, chat_id, arg):
        with self.state.lock:
            if chat_id in self.state.subscribers:
                self.state.subscribers.remove(chat_id)
            self.state.save()
        self.reply(chat_id, "👋 Unsubscribed.")

    @staticmethod
    def _count(arg: str, default: int = 5, cap: int = 15) -> int:
        try:
            return max(1, min(cap, int(arg)))
        except (TypeError, ValueError):
            return default

    def _show(self, chat_id, papers: list[Paper], empty: str):
        if not papers:
            self.reply(chat_id, empty)
            return
        self.send_papers(chat_id, papers, header=False)

    def cmd_latest(self, chat_id, arg):
        n = self._count(arg)
        papers = [p for p in self.arxiv.search(max_results=100) if self.cls.relevant(p)][:n]
        self._show(chat_id, papers, "No matching papers found.")

    def cmd_topics(self, chat_id, arg):
        lines = ["<b>Topics</b> (use /topic &lt;key&gt;)"]
        lines += [f"<code>{k}</code> — {esc(label)}" for k, label, _ in self.cls.topics]
        self.reply(chat_id, "\n".join(lines))

    def cmd_topic(self, chat_id, arg):
        parts = arg.split()
        keys = [k for k, _, _ in self.cls.topics]
        if not parts or parts[0].lower() not in keys:
            raise ValueError("Usage: /topic <key> [n]. Keys: " + ", ".join(keys))
        key, n = parts[0].lower(), self._count(parts[1] if len(parts) > 1 else None)
        papers = [p for p in self.arxiv.search(max_results=200)
                  if self.cls.relevant(p) and key in self.cls.topic_keys(p)][:n]
        self._show(chat_id, papers, f"No recent papers in <code>{key}</code>.")

    def cmd_search(self, chat_id, arg):
        tokens = [a or b for a, b in re.findall(r'"([^"]+)"|(\S+)', arg)]
        tokens = [t for t in (re.sub(r"[^\w\s\-+.]", "", t).strip() for t in tokens) if t]
        if not tokens:
            raise ValueError('Usage: /search <words>, e.g. /search "quantum memory" Ba')
        extra = " AND ".join(ArxivClient.term(t) for t in tokens)
        papers = [p for p in self.arxiv.search(extra=extra, max_results=50) if self.cls.relevant(p)][:8]
        self._show(chat_id, papers, "Nothing found for that search.")

    def cmd_check(self, chat_id, arg):
        subscribed = chat_id in self.chats()
        n = self.check_and_deliver()
        if not subscribed:
            self.reply(chat_id, "This chat isn't subscribed — use /subscribe first.")
        elif n == 0:
            self.reply(chat_id, "No new papers since the last check.")

    def cmd_status(self, chat_id, arg):
        last = self.state.last_check or "never"
        self.reply(chat_id, f"Last check: {esc(last)}\nSubscribed chats: {len(self.chats())}\n"
                            f"Papers remembered: {len(self.state.seen)}\n"
                            f"Check interval: {self.interval_h:g} h")

    # ---- main loop ----
    def scheduler(self, stop: threading.Event):
        while not stop.is_set():
            try:
                self.check_and_deliver()
            except Exception:
                log.exception("scheduled check failed")
            stop.wait(self.interval_h * 3600)

    def run(self):
        me = self.tg.call("getMe")
        log.info("running as @%s", me.get("username"))
        self.tg.call("deleteWebhook")
        try:
            self.tg.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
        except TelegramError as e:
            log.warning("setMyCommands failed: %s", e)

        stop = threading.Event()
        threading.Thread(target=self.scheduler, args=(stop,), daemon=True).start()
        offset = None
        try:
            while True:
                try:
                    updates = self.tg.call("getUpdates", offset=offset, timeout=50,
                                           allowed_updates=["message", "channel_post"])
                except TelegramError as e:
                    log.error("getUpdates: %s", e)
                    time.sleep(10)
                    continue
                for u in updates:
                    offset = u["update_id"] + 1
                    threading.Thread(target=self.handle, args=(u,), daemon=True).start()
        except KeyboardInterrupt:
            log.info("stopping")
            stop.set()


# ───────────────────────────── entry point ─────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Trapped-ion arXiv → Telegram bot")
    ap.add_argument("mode", choices=["run", "once", "dry-run"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--days", type=float, help="dry-run: look back this many days (default 3)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    arxiv, cls = ArxivClient(cfg), Classifier(cfg)

    if args.mode == "dry-run":
        if args.days:
            cfg["schedule"]["first_run_lookback_days"] = args.days
        fresh, _, _ = collect_new(arxiv, cls, State(None), cfg)
        for p in fresh:
            print(to_plain(format_paper(p, cls, int(cfg["telegram"].get("abstract_chars", 600)))))
            print("─" * 70)
        print(f"{len(fresh)} relevant paper(s)")
        return

    token = os.environ.get(cfg["telegram"].get("token_env", "TELEGRAM_BOT_TOKEN"))
    if not token:
        sys.exit("Set the TELEGRAM_BOT_TOKEN environment variable (token from @BotFather).")
    bot = Bot(cfg, State(args.state), arxiv, cls, Telegram(token))

    if args.mode == "once":
        if not bot.chats():
            sys.exit("No chats to post to: set TELEGRAM_CHAT_ID or default_chat_ids in config.yaml.")
        n = bot.check_and_deliver()
        log.info("posted %d new paper(s)", n)
    else:
        bot.run()


if __name__ == "__main__":
    main()
