"""
Borg Queen Control - Web GUI & Swarm Server
Pickett Applied Technologies Laboratories (PATL)
"""

import argparse
import asyncio
import glob
import hashlib
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
import aiohttp
from aiohttp import web

from pdf_ingest import convert_pdf, markdown_is_current

STATIC_DIR = Path(__file__).parent / "static"
ZIP_FILE_PATH = Path(__file__).parent / "borg_queen.zip"

def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default

DEFAULT_VAULT_PATH = _env_path(
    "BORG_VAULT", Path.home() / "Documents/PATL/Lab_Second_brain/wiki"
)
DEFAULT_INBOX_PATH = _env_path(
    "BORG_INBOX", Path.home() / "Documents/PATL/Lab_Second_brain/inbox"
)

def get_effective_paths():
    v = DEFAULT_VAULT_PATH
    i = DEFAULT_INBOX_PATH
    v.mkdir(parents=True, exist_ok=True)
    i.mkdir(parents=True, exist_ok=True)
    return v, i

def find_available_port(preferred_port: int = 8082, max_attempts: int = 50) -> int:
    for p in range(preferred_port, preferred_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('0.0.0.0', p))
                return p
            except OSError:
                continue
    return preferred_port

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")

_cpu_prev = None  # (idle, total)
_gpu_cache = {"t": 0.0, "data": None}


def _read_cpu_times():
    with open("/proc/stat", "r", encoding="utf-8") as f:
        parts = f.readline().split()
    nums = [int(x) for x in parts[1:8]]
    idle = nums[3] + nums[4]
    return idle, sum(nums)


def _read_mem():
    info = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.strip().split()[0])  # kB
    total = info.get("MemTotal", 1)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = max(total - avail, 0)
    return {
        "total_gb": round(total / 1024 / 1024, 1),
        "used_gb": round(used / 1024 / 1024, 1),
        "avail_gb": round(avail / 1024 / 1024, 1),
        "pct": round(100.0 * used / total, 1),
    }


def _read_cpu_temp_c():
    """Ryzen Tctl from k10temp, else a labeled package sensor, else the first sane thermal zone."""
    hwmon = Path("/sys/class/hwmon")
    fallback = None
    if hwmon.is_dir():
        for dev in hwmon.iterdir():
            try:
                name = (dev / "name").read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                continue
            for inp in sorted(dev.glob("temp*_input")):
                label_path = inp.with_name(inp.name.replace("_input", "_label"))
                try:
                    label = label_path.read_text(encoding="utf-8", errors="ignore").strip() if label_path.exists() else ""
                    milli = int(inp.read_text(encoding="utf-8", errors="ignore").strip())
                except (OSError, ValueError):
                    continue
                celsius = milli / 1000.0
                if not 10.0 <= celsius <= 115.0:
                    continue
                if name == "k10temp" and label in ("Tctl", "Tdie", ""):
                    return round(celsius, 1)
                if label in ("Tctl", "Tdie", "Package id 0", "x86_pkg_temp") and fallback is None:
                    fallback = celsius
    if fallback is not None:
        return round(fallback, 1)
    zone = Path("/sys/class/thermal")
    if zone.is_dir():
        for temp in sorted(zone.glob("thermal_zone*/temp")):
            try:
                celsius = int(temp.read_text(encoding="utf-8", errors="ignore").strip()) / 1000.0
            except (OSError, ValueError):
                continue
            if 10.0 <= celsius <= 115.0:
                return round(celsius, 1)
    return None


def _read_gpu():
    now = time.time()
    if _gpu_cache["data"] is not None and (now - _gpu_cache["t"]) < 1.0:
        return _gpu_cache["data"]
    data = {"name": "GPU", "util_pct": None, "vram_used_gb": None, "vram_total_gb": None, "pct": None, "temp_c": None}
    try:
        import subprocess
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            timeout=0.6,
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="ignore").strip()
        if out:
            line = out.splitlines()[0]
            name, util, used, total, temp = [p.strip() for p in line.split(",")]
            used_gb = round(float(used) / 1024.0, 2)
            total_gb = round(float(total) / 1024.0, 2)
            temp_c = None
            try:
                temp_c = int(float(temp))
            except ValueError:
                temp_c = None
            data = {
                "name": name.replace("NVIDIA GeForce ", "").replace("NVIDIA ", ""),
                "util_pct": int(float(util)),
                "vram_used_gb": used_gb,
                "vram_total_gb": total_gb,
                "pct": round(100.0 * used_gb / total_gb, 1) if total_gb else None,
                "temp_c": temp_c,
            }
    except Exception:
        pass
    _gpu_cache["t"] = now
    _gpu_cache["data"] = data
    return data


def sample_host_metrics() -> Dict[str, Any]:
    """Cheap CPU / RAM / GPU snapshot for the dashboard strip."""
    global _cpu_prev
    cpu_pct = None
    load = None
    try:
        idle, total = _read_cpu_times()
        if _cpu_prev:
            di = idle - _cpu_prev[0]
            dt = total - _cpu_prev[1]
            if dt > 0:
                cpu_pct = round(100.0 * (1.0 - di / dt), 1)
        _cpu_prev = (idle, total)
        load = round(os.getloadavg()[0], 2)
    except Exception:
        pass
    mem = {}
    try:
        mem = _read_mem()
    except Exception:
        mem = {"total_gb": None, "used_gb": None, "pct": None}
    gpu = _read_gpu()
    cpu_temp = None
    try:
        cpu_temp = _read_cpu_temp_c()
    except Exception:
        cpu_temp = None
    return {
        "cpu_pct": cpu_pct,
        "cpu_temp_c": cpu_temp,
        "load1": load,
        "nproc": os.cpu_count() or 0,
        "mem": mem,
        "gpu": gpu,
    }

class SwarmEngine:
    def __init__(self, vault_path: Path, inbox_path: Path):
        self.vault_path = vault_path
        self.inbox_path = inbox_path
        self.is_running = False
        self.log_history = []
        self.processed_today = 0
        self.current_step = "IDLE"
        self.latest_briefing = ""
        self.config = {
            "scout_model": "qwen2.5vl:7b",
            "drone_model": "olmo-3:7b-think",
            "adversary_model": "zarigata/unfiltered-llama3:latest",
            "queen_model": "cogito:32b",
            "mode": "auto"
        }
        # --- v2.5 Model Preload (VRAM residency) tracking ---
        self.preloaded_models = set()   # models confirmed resident via /api/ps
        self.preloading = set()         # models currently loading in background
        self.preload_errors = {}        # model -> last error string
        self.init_index()

    def add_log(self, caste: str, message: str, level: str = "info"):
        entry = {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "caste": caste,
            "message": message,
            "level": level
        }
        self.log_history.append(entry)
        if len(self.log_history) > 200:
            self.log_history.pop(0)

    def init_index(self):
        db_path = self.vault_path / ".hive_index.sqlite"
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        with self.conn:
            self.conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                    path UNINDEXED,
                    title,
                    content,
                    tokenize = 'porter unicode61'
                );
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    path TEXT PRIMARY KEY,
                    mtime REAL
                );
            """)
        self._ensure_hash_column()

    def _ensure_hash_column(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(meta)")}
        if "content_hash" not in cols:
            with self.conn:
                self.conn.execute("ALTER TABLE meta ADD COLUMN content_hash TEXT")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_meta_hash ON meta(content_hash)")

    @staticmethod
    def note_content_hash(text: str) -> str:
        body = re.split(r"## 🐝 Hive Mind Knowledge Fabric", text or "", maxsplit=1)[0]
        body = re.sub(r"\s+", " ", body).strip().lower()
        return hashlib.sha256(body.encode("utf-8", errors="ignore")).hexdigest()

    def _backfill_hashes(self) -> int:
        missing = self.conn.execute(
            "SELECT path FROM meta WHERE content_hash IS NULL OR content_hash = ''"
        ).fetchall()
        n = 0
        with self.conn:
            for (path,) in missing:
                row = self.conn.execute("SELECT content FROM notes_fts WHERE path = ?", (path,)).fetchone()
                if not row:
                    continue
                self.conn.execute(
                    "UPDATE meta SET content_hash = ? WHERE path = ?",
                    (self.note_content_hash(row[0] or ""), path),
                )
                n += 1
        return n

    def sync_index(self, force: bool = False) -> int:
        cursor = self.conn.cursor()
        cursor.execute("SELECT path, mtime FROM meta")
        indexed = dict(cursor.fetchall())
        to_index = []

        ignored_parts = {".obsidian", ".trash", ".git", ".hive", ".stversions", ".smart-env"}
        all_mds = [
            p for p in self.vault_path.rglob("*.md")
            if not any(part in ignored_parts or part.startswith(".") for part in p.relative_to(self.vault_path).parts)
        ]

        for md in all_mds:
            str_path = str(md.relative_to(self.vault_path))
            try:
                mtime = md.stat().st_mtime
                if force or str_path not in indexed or indexed[str_path] < mtime:
                    content = md.read_text(encoding="utf-8", errors="ignore")
                    title = md.stem
                    to_index.append((str_path, title, content, mtime))
            except Exception:
                continue

        if to_index:
            with self.conn:
                for str_path, title, content, mtime in to_index:
                    self.conn.execute("DELETE FROM notes_fts WHERE path = ?", (str_path,))
                    self.conn.execute("INSERT INTO notes_fts(path, title, content) VALUES(?, ?, ?)", (str_path, title, content))
                    self.conn.execute(
                        "INSERT OR REPLACE INTO meta(path, mtime, content_hash) VALUES(?, ?, ?)",
                        (str_path, mtime, self.note_content_hash(content)),
                    )

        cursor.execute("SELECT count(*) FROM meta")
        count = cursor.fetchone()[0]
        return count

    def list_inbox_studies(self) -> List[Path]:
        if not self.inbox_path.exists():
            return []
        return sorted(
            (p for p in self.inbox_path.rglob("*.md") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def list_inbox_pdfs(self) -> List[Path]:
        if not self.inbox_path.exists():
            return []
        return sorted(
            (p for p in self.inbox_path.rglob("*.pdf") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def pending_inbox_count(self) -> int:
        notes = {p.stem for p in self.list_inbox_studies()}
        extra = 0
        for pdf in self.list_inbox_pdfs():
            if pdf.stem in notes and markdown_is_current(pdf):
                continue
            extra += 1
        return len(notes) + extra

    def convert_inbox_pdfs(self) -> List[Path]:
        """Write a Markdown note, tables, and figure files for each inbox PDF."""
        written = []
        for pdf in self.list_inbox_pdfs():
            if markdown_is_current(pdf):
                continue
            self.current_step = f"READING_PDF: {pdf.stem}"
            self.add_log("SCOUT", f"Reading PDF '{pdf.name}' into Markdown with tables and figures...", "info")
            try:
                md = convert_pdf(pdf)
            except Exception as exc:
                self.add_log("ERROR", f"PDF conversion failed for {pdf.name}: {exc}", "error")
                continue
            written.append(md)
            figures = list(pdf.parent.glob(f"{md.stem}-p*.png"))
            self.add_log(
                "INDEX",
                f"PDF ready: {md.name} with {len(figures)} figure file(s). Original kept as {pdf.name}.",
                "success",
            )
        return written

    def _sibling_assets(self, study: Path) -> List[Path]:
        assets = []
        for path in study.parent.glob(study.stem + "-*"):
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                assets.append(path)
        pdf = study.with_suffix(".pdf")
        if pdf.exists() and pdf.is_file():
            assets.append(pdf)
        return assets

    def planned_wiki_path(self, study: Path) -> Path:
        dest_root = self.vault_path / "inbox"
        try:
            rel = study.relative_to(self.inbox_path)
        except ValueError:
            rel = Path(study.name)
        return dest_root / rel

    def find_duplicate(self, study: Path, content: str) -> Optional[Dict[str, str]]:
        """Return wiki path + reason if this note is already in the vault."""
        h = self.note_content_hash(content)
        row = self.conn.execute(
            "SELECT path FROM meta WHERE content_hash = ? LIMIT 1", (h,)
        ).fetchone()
        if row:
            return {"reason": "same_content", "path": row[0], "hash": h}
        dest = self.planned_wiki_path(study)
        if dest.exists():
            existing = dest.read_text(encoding="utf-8", errors="ignore")
            if self.note_content_hash(existing) == h:
                return {"reason": "same_path_same_content", "path": str(dest.relative_to(self.vault_path)), "hash": h}
        stem = study.stem.lower()
        for (p,) in self.conn.execute("SELECT path FROM meta"):
            if Path(p).stem.lower() != stem:
                continue
            wiki_p = self.vault_path / p
            if not wiki_p.exists():
                return {"reason": "same_title", "path": p, "hash": h}
            existing = wiki_p.read_text(encoding="utf-8", errors="ignore")
            if self.note_content_hash(existing) == h:
                return {"reason": "same_title_same_content", "path": p, "hash": h}
            return {"reason": "same_title", "path": p, "hash": h}
        return None

    def promote_inbox_file(self, study: Path, content: Optional[str] = None) -> Optional[Path]:
        """Move an assimilated inbox note into the wiki, or drop it if it is a duplicate."""
        text = content if content is not None else study.read_text(encoding="utf-8", errors="ignore")
        dup = self.find_duplicate(study, text)
        self.last_duplicate = dup
        assets = self._sibling_assets(study)
        if dup and dup["reason"] in ("same_content", "same_path_same_content", "same_title_same_content"):
            for path in [study, *assets]:
                try:
                    path.unlink()
                except OSError:
                    pass
            return None
        dest = self.planned_wiki_path(study)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or (dup and dup["reason"] == "same_title"):
            dest = dest.with_name(f"{dest.stem}_{datetime.now().strftime('%H%M%S')}{dest.suffix}")
        study.replace(dest)
        for asset in assets:
            target = dest.parent / asset.name
            if target.exists():
                target = target.with_name(f"{target.stem}_{datetime.now().strftime('%H%M%S')}{target.suffix}")
            try:
                asset.replace(target)
            except OSError:
                pass
        return dest

    def index_file(self, md: Path) -> None:
        str_path = str(md.relative_to(self.vault_path))
        content = md.read_text(encoding="utf-8", errors="ignore")
        mtime = md.stat().st_mtime
        with self.conn:
            self.conn.execute("DELETE FROM notes_fts WHERE path = ?", (str_path,))
            self.conn.execute(
                "INSERT INTO notes_fts(path, title, content) VALUES(?, ?, ?)",
                (str_path, md.stem, content),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(path, mtime, content_hash) VALUES(?, ?, ?)",
                (str_path, mtime, self.note_content_hash(content)),
            )

    _FTS_STOP = frozenset("""
        a an the and or of to for from with without on in is are was were be been being
        this that these those it its as at by into over under than then so such
        what which who whom whose how why when where there here
        used use using have has had do does did can could should would may might
        some any all both few more most other into about after before between
        based only also not no nor if than per via vs
    """.split())

    @classmethod
    def content_terms(cls, query: str) -> List[str]:
        """Keep distinctive tokens including A5 / ferro-A5; drop stopwords."""
        raw = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{0,}", query or "")
        out = []
        seen = set()
        for tok in raw:
            low = tok.lower()
            if low in cls._FTS_STOP:
                continue
            if len(low) < 2 and not re.search(r"\d", tok):
                continue
            if low in seen:
                continue
            seen.add(low)
            out.append(tok)
        return out

    @staticmethod
    def _fts_quote(term: str) -> str:
        cleaned = re.sub(r"[^\w.+-]", " ", term).strip()
        return '"' + cleaned.replace('"', "") + '"'

    def _fts_match(self, sql: str, match: str, *params):
        cur = self.conn.cursor()
        try:
            cur.execute(sql, (match,) + params)
            return cur.fetchall()
        except sqlite3.OperationalError:
            return []

    def search_similar(self, query: str, limit: int = 8, exclude: str = "") -> List[str]:
        hits = self.search_with_snippets(query, limit=limit + 2)
        titles = []
        for h in hits:
            if exclude and h.get("path") == exclude:
                continue
            titles.append(h["title"])
            if len(titles) >= limit:
                break
        return titles

    @staticmethod
    def titles_within_relevance(scored: List[tuple], floor: float = 0.80) -> List[str]:
        """Keep titles whose FTS5 bm25 is at least `floor` of the best hit.

        bm25() is negative and better matches are more negative, so relevance
        against the best hit is rank/best. Rows must already be best-first.
        """
        usable = [(title, float(rank)) for title, rank in scored if rank is not None and float(rank) < 0 and title]
        if not usable:
            return []
        best = usable[0][1]
        titles = []
        seen = set()
        for title, rank in usable:
            if rank / best < floor:
                break
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            titles.append(title)
        return titles

    def related_note_titles(self, query: str, floor: float = 0.80, fetch: int = 48, exclude: str = "") -> List[str]:
        """Wiki titles at least `floor` as relevant as the best FTS hit. No fixed count."""
        terms = self.content_terms(query)
        if not terms:
            return []
        quoted = [self._fts_quote(t) for t in terms[:8]]
        match = " OR ".join(quoted) if len(quoted) > 1 else quoted[0]
        sql = """
            SELECT path, title, bm25(notes_fts) AS rank
            FROM notes_fts
            WHERE notes_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        scored = []
        for path, title, rank in self._fts_match(sql, match, fetch):
            if exclude and path == exclude:
                continue
            scored.append((title, rank))
        return self.titles_within_relevance(scored, floor)

    def search_with_snippets(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        terms = self.content_terms(query)
        if not terms:
            return []
        quoted = [self._fts_quote(t) for t in terms[:12]]
        attempts = []
        if len(quoted) >= 2:
            attempts.append(" AND ".join(quoted[:6]))
            attempts.append(" OR ".join(quoted[:8]))
        else:
            attempts.append(quoted[0])
        sql = """
            SELECT path, title, snippet(notes_fts, 2, '<b>', '</b>', '...', 18) as snip, content
            FROM notes_fts
            WHERE notes_fts MATCH ?
            ORDER BY bm25(notes_fts)
            LIMIT ?
        """
        rows = []
        seen = set()
        for match in attempts:
            for path, title, snip, content in self._fts_match(sql, match, limit):
                if path in seen:
                    continue
                seen.add(path)
                excerpt_n = 1600 if len(rows) < 12 else 900
                rows.append({
                    "path": path,
                    "title": title,
                    "snippet": snip,
                    "excerpt": content[:excerpt_n],
                })
                if len(rows) >= limit:
                    return rows
        return rows

    def plan_query_parts(self, query: str) -> List[Dict[str, str]]:
        """Split a vault question into 2–4 focused retrievals (no extra LLM)."""
        q = (query or "").strip()
        parts: List[Dict[str, str]] = [{"title": "Overview", "query": q}]
        clauses = re.split(r"\s*(?:,|;|/|\bwhich\b|\bthat\b|\band then\b)\s+", q, flags=re.I)
        seen = {q.lower()}
        n = 1
        for raw in clauses:
            clause = re.sub(r"\s+", " ", raw).strip(" .")
            if len(clause) < 16:
                continue
            key = clause.lower()
            if key in seen or key == q.lower():
                continue
            seen.add(key)
            n += 1
            parts.append({"title": f"Part {n - 1}", "query": clause})
            if len(parts) >= 3:
                break
        terms = self.content_terms(q)
        if terms:
            focused = " ".join(terms[:8])
            if focused.lower() not in seen:
                parts.append({"title": "Named terms", "query": focused})
        out = []
        seen_q = set()
        for p in parts:
            k = p["query"].lower()
            if k in seen_q:
                continue
            seen_q.add(k)
            out.append(p)
        return out[:4]

    @staticmethod
    def _context_from_sources(sources: List[Dict[str, Any]]) -> str:
        blocks = []
        for s in sources:
            blocks.append(f"### [[{s['title']}]]\n{s['excerpt']}")
        return "\n\n".join(blocks)

    QUERY_SYSTEM = (
        "You are the Borg Queen / Knowledge Synthesizer for Pickett Applied Technologies Laboratories. "
        "Answer ONLY the current section using the vault notes provided. "
        "Cite [[wikilinks]]. Extract named materials, substrates, and product codes. "
        "Ignore off-topic notes. Never say the vault lacks data if any provided note is relevant."
    )

    @staticmethod
    def is_heavy_ram_model(model: str) -> bool:
        m = (model or "").lower()
        return any(tag in m for tag in ("70b", "72b", "65b", "67b"))

    async def synthesize_sectioned(self, query: str, model: str, src_limit: int = 48, token_cb=None):
        """Retrieve 2–3× more notes, split the question, answer each part as a section."""
        loop = asyncio.get_running_loop()
        heavy = self.is_heavy_ram_model(model)
        # 70B/72B: one pass, short context. Four sectioned prefills will miss
        # first-token by minutes and trip socket timeouts.
        if heavy:
            parts = [{"title": "Overview", "query": query}]
            src_limit = min(src_limit, 10)
            per = 10
            max_tok = 280
        else:
            parts = self.plan_query_parts(query)
            per = max(16, min(28, src_limit // max(len(parts), 1) + 10))
            max_tok = 520
        union: List[Dict[str, Any]] = []
        seen = set()
        fts_t0 = time.time()
        for p in parts:
            hits = await loop.run_in_executor(None, self.search_with_snippets, p["query"], per)
            p["sources"] = hits
            for h in hits:
                if h["path"] not in seen:
                    seen.add(h["path"])
                    union.append(h)
        fts_ms = round((time.time() - fts_t0) * 1000, 1)
        if token_cb:
            await token_cb({"event": "plan", "parts": [{"title": p["title"], "query": p["query"]} for p in parts],
                            "sources": union, "fts_ms": fts_ms})
        chunks: List[str] = []
        for i, p in enumerate(parts):
            heading = ("\n" if i else "") + f"## {p['title']}\nFocus: {p['query']}\n\n"
            chunks.append(heading)
            if token_cb:
                await token_cb({"event": "section", "title": p["title"], "index": i + 1, "total": len(parts)})
                await token_cb({"event": "token", "text": heading})
            ctx = self._context_from_sources((p.get("sources") or [])[: (8 if heavy else 20)])
            user_prompt = (
                f"Original question:\n{query}\n\n"
                f"Section {i + 1}/{len(parts)} — {p['title']}\nFocus:\n{p['query']}\n\n"
                f"Vault evidence ({len(p.get('sources') or [])} notes):\n{ctx}\n\n"
                f"Write this section only, in complete sentences, with [[citations]]."
            )
            async for tok in self.iter_inference_tokens(model, self.QUERY_SYSTEM, user_prompt, max_tokens=max_tok):
                chunks.append(tok)
                if token_cb:
                    await token_cb({"event": "token", "text": tok})
        return "".join(chunks).strip(), union, fts_ms, parts

    async def get_loaded_models(self) -> List[str]:
        """Query Ollama /api/ps for currently VRAM/RAM-resident models. Fast (~50ms)."""
        url = f"{OLLAMA_BASE_URL}/api/ps"
        def _ps():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "BorgQueen/1.0"})
                with urllib.request.urlopen(req, timeout=2) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return [m.get("name", "") for m in data.get("models", [])]
            except Exception:
                return []
        loaded = await asyncio.get_running_loop().run_in_executor(None, _ps)
        # sync our resident set with ground truth
        for m in loaded:
            self.preloaded_models.add(m)
        # drop stale entries that ollama evicted
        for m in list(self.preloaded_models):
            if m not in loaded and m not in self.preloading:
                # keep entries whose short name matches (ollama may return :latest suffix variants)
                if not any(m.split(":")[0] in l for l in loaded):
                    pass  # keep conservative: don't drop on transient empty ps
        return loaded

    async def preload_model(self, model: str, background: bool = False) -> Dict[str, Any]:
        """Preload a model into VRAM/RAM with keep_alive so queries answer instantly.
        Sends a 1-token warmup generation. cogito:32b (~20GB Q4) takes ~1-2 min cold,
        olmo-3:7b (~4.5GB) ~10-20s. Safe to call repeatedly; concurrent calls coalesce.
        """
        if model in self.preloading:
            return {"status": "loading", "model": model}
        # Fast path: already resident?
        try:
            loaded = await self.get_loaded_models()
            if any(model.split(":")[0] in (l or "") for l in loaded) or model in self.preloaded_models:
                self.preloaded_models.add(model)
                return {"status": "hot", "model": model, "resident": loaded}
        except Exception:
            pass

        is_live = await self.check_ollama()
        if not is_live:
            self.add_log("SYSTEM", f"Preload of {model} skipped: Ollama offline (simulation mode).", "warning")
            return {"status": "offline", "model": model}

        async def _do_load():
            self.preloading.add(model)
            self.add_log("QUEEN", f"Preloading {model} into VRAM/RAM (keep_alive=1h)... first load ~60-90s for 14B, GPU spike is normal.", "info")
            payload = {
                "model": model,
                "prompt": "You are the Borg Queen. Reply with exactly: ONLINE",
                "stream": False,
                "keep_alive": "1h",
                "options": {"num_predict": 1, "temperature": 0.1}
            }
            def _call():
                req = urllib.request.Request(
                    f"{OLLAMA_BASE_URL}/api/generate",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                # Cold load of 9GB 14B weights can take minutes on first touch
                with urllib.request.urlopen(req, timeout=600) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _call)
                self.preloaded_models.add(model)
                self.preload_errors.pop(model, None)
                self.add_log("QUEEN", f"{model} is HOT and VRAM/RAM-resident. Transmit queries will now answer instantly.", "success")
            except Exception as e:
                err = str(e)
                self.preload_errors[model] = err
                self.add_log("ERROR", f"Preload of {model} failed: {err[:200]}", "error")
            finally:
                self.preloading.discard(model)

        if background:
            asyncio.create_task(_do_load())
            return {"status": "loading", "model": model}
        else:
            await _do_load()
            if model in self.preloaded_models:
                return {"status": "hot", "model": model}
            return {"status": "error", "model": model, "error": self.preload_errors.get(model, "unknown")}

    async def check_ollama(self) -> bool:
        url = f"{OLLAMA_BASE_URL}/api/tags"
        try:
            timeout = aiohttp.ClientTimeout(total=2.5, sock_connect=1.5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={"User-Agent": "BorgQueen/1.0"}) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def iter_inference_tokens(self, model: str, system: str, prompt: str, max_tokens: int = 250):
        """Yield token strings from Ollama with stream=true so first-byte is fast."""
        if self.config["mode"] == "simulation":
            text = await self._simulated_inference(system)
            yield text
            return
        # sock_read=None: 70B/72B first token can take 6+ minutes (RAM prefill).
        # A 180s read timeout caused chat to fail then generate to fail again = ~360s.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
        heavy = self.is_heavy_ram_model(model)
        options = {
            "num_predict": max_tokens,
            "temperature": 0.3,
            "num_ctx": 4096 if heavy else 8192,
        }
        chat_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": True,
            "keep_alive": "1h",
            "options": options,
        }
        last_err = None
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.post(
                    f"{OLLAMA_BASE_URL}/api/chat",
                    json=chat_payload,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    if resp.status != 200:
                        last_err = f"Ollama HTTP {resp.status}: {(await resp.text())[:300]}"
                    else:
                        async for raw in resp.content:
                            line = raw.decode("utf-8", errors="ignore").strip()
                            if not line:
                                continue
                            if line.startswith("data:"):
                                line = line[5:].strip()
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            piece = (
                                ((obj.get("message") or {}).get("content"))
                                or obj.get("response")
                                or ""
                            )
                            if piece:
                                yield piece
                            if obj.get("done"):
                                self.preloaded_models.add(model)
                                return
                        self.preloaded_models.add(model)
                        return
            except Exception as e:
                last_err = str(e)
        raise RuntimeError(f"Ollama inference ({model}) failed: {last_err}")

    async def run_inference(self, model: str, system: str, prompt: str, max_tokens: int = 250) -> str:
        parts = []
        async for tok in self.iter_inference_tokens(model, system, prompt, max_tokens):
            parts.append(tok)
        return "".join(parts).strip()

    async def _simulated_inference(self, system: str) -> str:
        await asyncio.sleep(0.05)
        if "query" in system.lower() or "synthesizer" in system.lower():
            return ("Based on your indexed vault notes, here is the synthesis:\n\n"
                    "1. **Core Findings**: The laboratory baseline establishes high wear resistance under cyclic thermal-mechanical load. Key protocols in [[Solid_State_Batteries_Protocols]] and [[High_Entropy_Alloys_MOC]] emphasize maintaining interfacial resistance < 15 Ohm-cm².\n"
                    "2. **Critical Variables**: Recent studies indicate ALD coatings reduce boundary voiding by 60%.\n"
                    "3. **Recommendation**: Validate test specimens under ambient atmospheric humidity before advancing to prototype fabrication.")
        elif "triage" in system.lower() or "scout" in system.lower():
            return "Core finding: Substantial reduction in interfacial degradation. Key terms: tribology, electrolyte, impedance, degradation, refractory."
        elif "graph" in system.lower() or "cross" in system.lower():
            return "- Direct correlation with established baseline in [[High_Entropy_Alloys_MOC]].\n- Overcomes critical current density barriers documented in [[Solid_State_Batteries_Protocols]]."
        elif "skeptical" in system.lower() or "adversary" in system.lower():
            return "Methodological caveat: Bench testing conducted in inert argon atmosphere; real-world ambient humidity was not evaluated."
        return "Analysis complete."

    async def assimilate_batch(self):
        if self.is_running:
            return
        self.is_running = True
        self.current_step = "SCANNING_INBOX"
        self.add_log("QUEEN", "Borg Queen Command initiated inbox assimilation.", "info")

        try:
            total_notes = self.sync_index()
            hashed = self._backfill_hashes()
            self.add_log("SYSTEM", f"SQLite FTS5 Knowledge Fabric active: {total_notes} wiki notes indexed.", "success")
            if hashed:
                self.add_log("INDEX", f"Backfilled content hashes for {hashed} existing notes (duplicate detection).", "info")

            self.convert_inbox_pdfs()
            studies = self.list_inbox_studies()
            if not studies:
                self.add_log("SCOUT", f"No pending studies found in {self.inbox_path}. Swarm resting.", "warning")
                self.current_step = "IDLE"
                self.is_running = False
                return

            self.add_log("QUEEN", f"Identified {len(studies)} studies in inbox awaiting assimilation.", "info")
            summaries = []

            for i, study in enumerate(studies, 1):
                self.current_step = f"ASSIMILATING [{i}/{len(studies)}]: {study.stem}"
                raw_text = study.read_text(encoding="utf-8", errors="ignore")

                dup = self.find_duplicate(study, raw_text)
                if dup and dup["reason"] != "same_title":
                    try:
                        study.unlink()
                    except OSError:
                        pass
                    self.add_log(
                        "INDEX",
                        f"Duplicate skipped: {study.name} already in wiki as [[{Path(dup['path']).stem}]] ({dup['reason']}). Inbox copy removed.",
                        "warning",
                    )
                    continue

                if "## 🐝 Hive Mind Knowledge Fabric" in raw_text:
                    wiki_note = self.promote_inbox_file(study, raw_text)
                    if wiki_note is None:
                        self.add_log("INDEX", f"Duplicate skipped after hive check: {study.name}", "warning")
                        continue
                    self.index_file(wiki_note)
                    self.add_log("INDEX", f"Already assimilated — moved to wiki: {wiki_note.relative_to(self.vault_path)}", "success")
                    summaries.append({"title": wiki_note.stem, "thesis": "(previously assimilated)", "related": []})
                    self.processed_today += 1
                    continue

                self.add_log("SCOUT", f"Scouting '{study.name}' with {self.config['scout_model']} (In VRAM)...", "info")
                scout_out = await self.run_inference(
                    self.config["scout_model"],
                    "You are a research triage scout. Extract core thesis and 5 domain keywords.",
                    raw_text[:8000]
                )

                related = self.related_note_titles(scout_out, floor=0.80)
                self.add_log(
                    "INDEX",
                    f"Cross-referenced against wiki notes: {len(related)} notes at ≥80% of the best match: {', '.join(related)}",
                    "success",
                )

                self.add_log("DRONE", f"Synthesizing [[wikilinks]] with {self.config['drone_model']}...", "info")
                cross_out = await self.run_inference(
                    self.config["drone_model"],
                    "You are a second-brain knowledge graph architect. Generate bullet points with [[wikilinks]]. Link every related note that was provided.",
                    f"Study: {scout_out}\nRelated Notes: {', '.join(related)}",
                    max_tokens=400,
                )

                self.add_log("ADVERSARY", f"Red-teaming study validity with {self.config['adversary_model']}...", "warning")
                adv_out = await self.run_inference(
                    self.config["adversary_model"],
                    "You are a skeptical peer reviewer. Find the weakness in 1-2 sharp sentences.",
                    scout_out
                )

                if "## 🐝 Hive Mind Knowledge Fabric" not in raw_text:
                    hive_block = f"""

---
## 🐝 Hive Mind Knowledge Fabric
**Core Thesis**: {scout_out}

### 🔗 Cross-Vault Connections
{cross_out}

### ⚠️ Epistemic & Methodological Caveats
{adv_out}
*Assimilated by PATL Borg Queen Swarm: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""
                    with open(study, "a", encoding="utf-8") as f:
                        f.write(hive_block)
                    self.add_log("SYSTEM", f"Assimilated: {study.name} enriched with [[wikilinks]] & caveats.", "success")

                wiki_note = self.promote_inbox_file(study)
                if wiki_note is None:
                    dup = getattr(self, "last_duplicate", None) or {}
                    self.add_log(
                        "INDEX",
                        f"Duplicate skipped: {study.name} already at [[{Path(dup.get('path', '')).stem}]]",
                        "warning",
                    )
                    continue
                self.index_file(wiki_note)
                note = f"Promoted to wiki: {wiki_note.relative_to(self.vault_path)} (Obsidian can open this note)."
                if getattr(self, "last_duplicate", None) and self.last_duplicate.get("reason") == "same_title":
                    note += f" Title collided with [[{Path(self.last_duplicate['path']).stem}]] — kept both under a timestamped name."
                self.add_log("INDEX", note, "success")

                summaries.append({
                    "title": wiki_note.stem,
                    "thesis": scout_out,
                    "related": related
                })
                self.processed_today += 1

            self.current_step = "QUEEN_SYNTHESIS"
            self.add_log("QUEEN", f"Awakening {self.config['queen_model']} for daily master briefing...", "info")
            queen_prompt = f"Summaries of {len(summaries)} new studies from inbox:\n" + "\n".join([f"- [[{s['title']}]]: {s['thesis']}" for s in summaries])
            queen_brief = await self.run_inference(
                self.config["queen_model"],
                "You are the Borg Queen / Master Knowledge Synthesizer. Write an authoritative daily lab briefing with strategic directives.",
                queen_prompt,
                max_tokens=400
            )

            today_str = datetime.now().strftime("%Y-%m-%d")
            brief_file = self.vault_path / f"Daily_Hive_Synthesis_{today_str}.md"
            full_brief = f"""---
tags: [hive-mind, daily-synthesis, patl, research-intake]
date: {today_str}
studies_assimilated: {len(summaries)}
source_inbox: {self.inbox_path}
---
# 👑 PATL Daily Hive Mind Synthesis — {today_str}

{queen_brief}

## 📥 Studies Assimilated From Inbox
""" + "\n".join([f"- [[{s['title']}]] — *Cross-linked with:* {', '.join([f'[[{r}]]' for r in s['related']])}" for s in summaries])

            brief_file.write_text(full_brief, encoding="utf-8")
            self.index_file(brief_file)
            self.latest_briefing = full_brief
            self.add_log("QUEEN", f"Master Briefing written to {brief_file.name} (wiki + FTS).", "success")
            self.current_step = "IDLE"

        except Exception as e:
            self.add_log("ERROR", f"Assimilation error: {str(e)}", "error")
            self.current_step = "ERROR"
        finally:
            self.is_running = False

# ---------------------------------------------------------------------------
# WEB APPLICATION & API
# ---------------------------------------------------------------------------
v_path, i_path = get_effective_paths()
swarm = SwarmEngine(v_path, i_path)

async def on_app_startup(app):
    async def _bg_index():
        await asyncio.sleep(0.5)
        swarm.add_log("INDEX", f"Starting automatic background scan of {swarm.vault_path}...", "info")
        loop = asyncio.get_running_loop()
        start_t = time.time()
        try:
            count = await loop.run_in_executor(None, swarm.sync_index)
            elapsed = time.time() - start_t
            swarm.add_log("INDEX", f"SQLite FTS5 Knowledge Fabric fully synchronized: {count} notes indexed in {elapsed:.2f}s.", "success")
        except Exception as e:
            swarm.add_log("ERROR", f"Initial index error: {e}", "error")
        # v2.5: auto-preload the Queen reasoning core so the first TRANSMIT is instant.
        # Runs in background — dashboard stays responsive while 9GB weights stream in.
        try:
            await asyncio.sleep(1.0)
            ollama_ok = await swarm.check_ollama()
            if ollama_ok:
                swarm.add_log("QUEEN", f"Auto-preloading reasoning core {swarm.config['queen_model']} (GPU spike + ~60-90s is normal on cold start)...", "info")
                await swarm.preload_model(swarm.config["queen_model"], background=True)
            else:
                swarm.add_log("SYSTEM", "Ollama offline at startup — running in Autonomous Simulation mode. Start Ollama and press PRELOAD.", "warning")
        except Exception as e:
            swarm.add_log("ERROR", f"Auto-preload error: {e}", "error")
    asyncio.create_task(_bg_index())

async def handle_index(request):
    html_content = generate_dashboard_html()
    return web.Response(
        text=html_content, 
        content_type="text/html", 
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    )

async def handle_status(request):
    ollama_ok = await swarm.check_ollama()
    def _count_notes():
        try:
            cur = swarm.conn.cursor()
            cur.execute("SELECT count(*) FROM meta")
            return cur.fetchone()[0]
        except Exception:
            return 0

    total_notes = await asyncio.get_running_loop().run_in_executor(None, _count_notes)
    pending = 0
    if swarm.inbox_path.exists():
        pending = await asyncio.get_running_loop().run_in_executor(
            None, swarm.pending_inbox_count
        )

    # v2.5: lightweight residency snapshot (2s timeout, never blocks dashboard poll)
    loaded_models = []
    if ollama_ok:
        try:
            loaded_models = await asyncio.wait_for(swarm.get_loaded_models(), timeout=2.5)
        except Exception:
            loaded_models = list(swarm.preloaded_models)

    return web.json_response({
        "is_running": swarm.is_running,
        "current_step": swarm.current_step,
        "ollama_online": ollama_ok,
        "vault_path": str(swarm.vault_path),
        "inbox_path": str(swarm.inbox_path),
        "total_notes": total_notes,
        "pending_studies": pending,
        "processed_today": swarm.processed_today,
        "config": swarm.config,
        "logs": swarm.log_history[-40:],
        "latest_briefing": swarm.latest_briefing,
        "loaded_models": loaded_models,
        "preloading": sorted(list(swarm.preloading)),
        "preload_errors": swarm.preload_errors,
        "host": sample_host_metrics(),
    })

async def handle_reindex(request):
    swarm.add_log("INDEX", "Manual re-index initiated by user...", "info")
    loop = asyncio.get_running_loop()
    start_t = time.time()
    try:
        count = await loop.run_in_executor(None, lambda: swarm.sync_index(force=True))
        elapsed = time.time() - start_t
        swarm.add_log("INDEX", f"Re-index complete: {count} notes verified/indexed in {elapsed:.2f}s.", "success")
        return web.json_response({"status": "success", "count": count, "elapsed": elapsed})
    except Exception as e:
        swarm.add_log("ERROR", f"Re-index failed: {e}", "error")
        return web.json_response({"status": "error", "message": str(e)}, status=500)

async def handle_preload(request):
    """POST /api/preload {model?} — pin a reasoning core HOT in VRAM/RAM.
    Returns immediately with {status: loading|hot|offline}; frontend polls
    /api/status (loaded_models) until the model shows HOT."""
    try:
        try:
            data = await request.json()
        except Exception:
            data = {}
        model = (data.get("model") or swarm.config["queen_model"]).strip()
        swarm.config["queen_model"] = model  # remember last selected core
        result = await swarm.preload_model(model, background=True)
        return web.json_response(result)
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=500)

async def handle_ps(request):
    """GET /api/ps — list Ollama-resident models + preload state."""
    try:
        loaded = await swarm.get_loaded_models()
        return web.json_response({
            "loaded": loaded,
            "preloading": sorted(list(swarm.preloading)),
            "preloaded": sorted(list(swarm.preloaded_models)),
            "errors": swarm.preload_errors
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_trigger(request):
    if swarm.is_running:
        return web.json_response({"status": "already_running"})
    asyncio.create_task(swarm.assimilate_batch())
    return web.json_response({"status": "initiated"})

async def handle_query_hive(request):
    """Commune with the Queen: Prompt box endpoint searching 5,000+ notes and generating synthesized answer."""
    t0 = time.time()
    query, model, sources = "", swarm.config["queen_model"], []
    try:
        data = await request.json()
        query = data.get("query", "").strip()
        model = data.get("model", swarm.config["queen_model"])
        if not query:
            return web.json_response({"error": "Query cannot be empty"}, status=400)

        swarm.add_log("QUERY", f"Vault Query: '{query[:60]}...' dispatched to {model}", "info")
        print(f"[QUERY] dispatch model={model!r} q={query[:80]!r}", flush=True)

        src_limit = data.get("limit") or 48
        try:
            src_limit = max(12, min(int(src_limit), 80))
        except (TypeError, ValueError):
            src_limit = 48

        want_stream = bool(data.get("stream")) or "text/event-stream" in (request.headers.get("Accept") or "")
        infer_t0 = time.time()
        if want_stream:
            resp = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "text/event-stream; charset=utf-8",
                    "Cache-Control": "no-cache, no-store",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
            await resp.prepare(request)

            async def send(obj):
                await resp.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))

            async def _keepalive():
                try:
                    while True:
                        await asyncio.sleep(8)
                        await resp.write(b": keepalive\n\n")
                except Exception:
                    return

            ka = asyncio.create_task(_keepalive())
            try:
                answer, sources, fts_ms, parts = await swarm.synthesize_sectioned(
                    query, model, src_limit=src_limit, token_cb=send
                )
            except Exception as infer_err:
                elapsed = round(time.time() - t0, 1)
                swarm.add_log("ERROR", f"Inference failed after {elapsed}s: {infer_err}", "error")
                await send({"event": "error", "error": str(infer_err), "elapsed_s": elapsed})
                await resp.write_eof()
                return resp
            finally:
                ka.cancel()
            infer_s = round(time.time() - infer_t0, 1)
            elapsed = round(time.time() - t0, 1)
            swarm.add_log("QUEEN", f"Sectioned synthesis '{query[:40]}...' in {elapsed}s ({len(parts)} parts, {len(sources)} notes)", "success")
            await send({
                "event": "done",
                "query": query, "model": model,
                "answer": answer,
                "sources": sources, "fts_ms": fts_ms, "infer_s": infer_s, "elapsed_s": elapsed,
                "parts": [{"title": p["title"], "query": p["query"]} for p in parts],
            })
            await resp.write_eof()
            return resp

        try:
            answer, sources, fts_ms, parts = await swarm.synthesize_sectioned(
                query, model, src_limit=src_limit, token_cb=None
            )
        except Exception as infer_err:
            elapsed = round(time.time() - t0, 1)
            swarm.add_log("ERROR", f"Inference failed after {elapsed}s for '{query[:40]}...': {infer_err}", "error")
            return web.json_response({
                "query": query, "model": model, "answer": "",
                "sources": sources, "elapsed_s": elapsed,
                "error": f"Inference failed after {elapsed}s: {infer_err}"
            }, status=500)
        infer_s = round(time.time() - infer_t0, 1)
        elapsed = round(time.time() - t0, 1)
        swarm.add_log("QUEEN", f"Synthesis generated for '{query[:40]}...' in {elapsed}s (FTS {fts_ms}ms + infer {infer_s}s)", "success")

        return web.json_response({
            "query": query,
            "model": model,
            "answer": answer,
            "sources": sources,
            "fts_ms": fts_ms,
            "infer_s": infer_s,
            "elapsed_s": elapsed,
            "parts": [{"title": p["title"], "query": p["query"]} for p in parts],
        })
    except Exception as e:
        elapsed = round(time.time() - t0, 1)
        return web.json_response({
            "query": query, "model": model, "answer": "",
            "sources": sources, "elapsed_s": elapsed,
            "error": str(e)
        }, status=500)

async def handle_diag(request):
    """GET /api/diag — one-shot triage: proves backend+index+ollama health."""
    t0 = time.time()
    ollama_ok = await swarm.check_ollama()
    loaded = []
    if ollama_ok:
        try:
            loaded = await asyncio.wait_for(swarm.get_loaded_models(), timeout=2.5)
        except Exception as e:
            loaded = [f"<ps-error: {e}>"]
    total_notes = 0
    try:
        cur = swarm.conn.cursor()
        cur.execute("SELECT count(*) FROM meta")
        total_notes = cur.fetchone()[0]
    except Exception:
        pass
    pending = swarm.pending_inbox_count()
    # live FTS self-test
    try:
        fts_hits = len(swarm.search_with_snippets("antenna metamaterial", limit=3))
    except Exception as e:
        fts_hits = f"<fts-error: {e}>"
    return web.json_response({
        "version": "v2.15 HEAVY",
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ollama_online": ollama_ok,
        "ollama_url": OLLAMA_BASE_URL,
        "loaded_models": loaded,
        "preloading": sorted(list(swarm.preloading)),
        "total_notes": total_notes,
        "pending_studies": pending,
        "queen_model": swarm.config["queen_model"],
        "fts_selftest_hits": fts_hits,
        "diag_elapsed_s": round(time.time() - t0, 2)
    })

async def handle_save_to_obsidian(request):
    """Saves a synthesized query result directly into the wiki vault as a note."""
    try:
        data = await request.json()
        title = data.get("title", f"Hive_Query_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        content = data.get("content", "")
        clean_title = re.sub(r'[^a-zA-Z0-9_\- ]', '', title).replace(' ', '_')
        file_path = swarm.vault_path / f"{clean_title}.md"

        full_note = f"""---
tags: [hive-mind, synthesis, patl-research]
date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
generated_by: borg_queen
---
# 🧠 {title.replace('_', ' ')}

{content}

---
*Exported from Borg Queen Control to Pickett Applied Tech Lab Wiki*
"""
        file_path.write_text(full_note, encoding="utf-8")
        swarm.sync_index()
        swarm.add_log("SYSTEM", f"Saved note to Obsidian: [[{clean_title}]]", "success")
        return web.json_response({"status": "saved", "title": clean_title, "path": str(file_path)})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_get_studies(request):
    all_studies = []
    if swarm.inbox_path.exists():
        for f in swarm.list_inbox_studies()[:200]:
            content = f.read_text(encoding="utf-8", errors="ignore")
            all_studies.append({
                "name": str(f.relative_to(swarm.inbox_path)),
                "title": f.stem,
                "size": len(content),
                "has_hive_fabric": "## 🐝 Hive Mind Knowledge Fabric" in content,
                "preview": content[:400]
            })
    return web.json_response(all_studies)

async def handle_get_study_content(request):
    name = request.query.get("name", "")
    target = swarm.inbox_path / name
    if not (target.exists() and target.is_file()):
        target = swarm.vault_path / name
    if target.exists() and target.is_file():
        return web.Response(text=target.read_text(encoding="utf-8", errors="ignore"), content_type="text/markdown")
    return web.Response(status=404, text="Note not found")

async def handle_download_zip(request):
    if ZIP_FILE_PATH.exists():
        return web.FileResponse(path=ZIP_FILE_PATH, headers={
            "Content-Disposition": 'attachment; filename="borg_queen.zip"'
        })
    return web.Response(status=404, text="Zip archive not found.")

async def handle_favicon(_request):
    icon = STATIC_DIR / "borg-queen-icon.png"
    if not icon.exists():
        return web.Response(status=404)
    return web.FileResponse(icon, headers={"Cache-Control": "no-cache"})


def generate_dashboard_html():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BORG QUEEN CONTROL // PATL</title>
<link rel="icon" type="image/png" href="/static/borg-queen-icon.png">
<style>
:root {
  --bg-dark: #070a08;
  --bg-card: #0d1510;
  --border-green: #0f3d26;
  --neon-green: #00ff66;
  --neon-green-glow: rgba(0, 255, 102, 0.35);
  --neon-dim: #059669;
  --text-main: #e2e8f0;
  --text-dim: #94a3b8;
  --accent-amber: #f59e0b;
}

* { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'JetBrains Mono', monospace; }
body { background: var(--bg-dark); color: var(--text-main); line-height: 1.5; min-height: 100vh; overflow-x: hidden; }

body::before {
  content: "";
  position: fixed;
  top: 0; left: 0; width: 100vw; height: 100vh;
  background-image: 
    linear-gradient(rgba(0, 255, 102, 0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0, 255, 102, 0.03) 1px, transparent 1px);
  background-size: 30px 30px;
  pointer-events: none;
  z-index: -1;
}

header {
  background: rgba(13, 21, 16, 0.95);
  border-bottom: 2px solid var(--border-green);
  padding: 12px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  backdrop-filter: blur(10px);
  position: sticky;
  top: 0;
  z-index: 100;
  box-shadow: 0 4px 25px rgba(0,0,0,0.5);
}

.brand-left { display: flex; align-items: center; gap: 16px; }
.brand-logo-patl { height: 50px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.1); }
.brand-logo-queen { height: 58px; border-radius: 6px; border: 1px solid var(--border-green); box-shadow: 0 0 15px var(--neon-green-glow); }

.brand-text h1 { font-size: 1.35rem; letter-spacing: 2px; font-weight: 800; color: #fff; display: flex; align-items: center; gap: 10px; }
.brand-text h1 span { color: var(--neon-green); text-shadow: 0 0 12px var(--neon-green); }
.brand-text p { font-size: 0.75rem; color: var(--text-dim); text-transform: uppercase; letter-spacing: 1.5px; }

.header-status-pills { display: flex; align-items: center; gap: 12px; }
.pill { font-size: 0.75rem; padding: 6px 12px; border-radius: 4px; background: #111a14; border: 1px solid var(--border-green); display: flex; align-items: center; gap: 6px; font-family: monospace; }
.pill-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--neon-green); box-shadow: 0 0 8px var(--neon-green); }
.pill-dot.offline { background: var(--accent-amber); box-shadow: 0 0 8px var(--accent-amber); }

.download-btn { background: #15261d; color: var(--neon-green); border: 1px solid var(--neon-dim); padding: 6px 12px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; text-decoration: none; display: flex; align-items: center; gap: 6px; transition: all 0.2s; }
.download-btn:hover { background: var(--neon-dim); color: #000; box-shadow: 0 0 10px var(--neon-green-glow); }

.container { max-width: 1400px; margin: 0 auto; padding: 24px; }

/* Telemetry Grid */
.telemetry-grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 8px; margin-bottom: 16px; }
@media (max-width: 1100px) { .telemetry-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
@media (max-width: 720px) { .telemetry-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.telemetry-card { background: var(--bg-card); border: 1px solid var(--border-green); border-radius: 8px; padding: 8px 10px; position: relative; overflow: hidden; transition: all 0.2s ease; min-width: 0; }
.telemetry-card:hover { border-color: var(--neon-green); box-shadow: 0 0 15px rgba(0,255,102,0.15); }
.telemetry-card::before { content: ""; position: absolute; top: 0; left: 0; width: 3px; height: 100%; background: var(--neon-dim); }
.telemetry-card.hw::before { background: #38bdf8; }
.telemetry-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 2px; }
.telemetry-label { font-size: 0.62rem; text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hw-bar { height: 4px; background: #122018; border-radius: 2px; margin-top: 6px; overflow: hidden; }
.hw-bar > span { display: block; height: 100%; background: var(--neon-green); width: 0%; transition: width 0.4s ease; }
.hw-bar.warn > span { background: var(--accent-amber); }
.hw-bar.hot > span { background: #f87171; }
.reindex-mini-btn { background: #112217; border: 1px solid var(--border-green); color: var(--neon-green); font-size: 0.68rem; font-weight: bold; padding: 2px 8px; border-radius: 4px; cursor: pointer; transition: all 0.2s; }
.reindex-mini-btn:hover:not(:disabled) { background: var(--neon-green); color: #000; }
.telemetry-val { font-size: 1.15rem; font-weight: 700; color: #fff; font-family: monospace; line-height: 1.2; }
.telemetry-sub { font-size: 0.65rem; color: var(--neon-green); margin-top: 2px; font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* COMMUNE WITH THE BORG QUEEN (PROMPT BOX) */
.queen-query-panel {
  background: #09120c;
  border: 2px solid var(--neon-dim);
  border-radius: 8px;
  padding: 20px;
  margin-bottom: 24px;
  box-shadow: 0 0 25px rgba(0, 255, 102, 0.15);
  position: relative;
}

.query-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.query-header h2 {
  font-size: 1.1rem;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  color: #fff;
  display: flex;
  align-items: center;
  gap: 10px;
}

.query-header h2 span { color: var(--neon-green); }

.model-picker-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.78rem;
  color: var(--text-dim);
  font-family: monospace;
}

.model-picker-select {
  background: #111a14;
  border: 1px solid var(--border-green);
  color: #fff;
  padding: 4px 8px;
  border-radius: 4px;
  font-family: monospace;
  font-size: 0.78rem;
  outline: none;
}

.preload-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: 12px;
  padding-left: 12px;
  border-left: 1px solid var(--border-green);
}
.preload-dot { width: 10px; height: 10px; border-radius: 50%; background: #475569; box-shadow: 0 0 6px #475569; transition: all 0.3s; }
.preload-dot.hot { background: var(--neon-green); box-shadow: 0 0 10px var(--neon-green); }
.preload-dot.loading { background: var(--accent-amber); box-shadow: 0 0 10px var(--accent-amber); animation: pulse-dot 1s infinite alternate; }
@keyframes pulse-dot { from { opacity: 0.4; } to { opacity: 1; } }
.preload-status { font-size: 0.72rem; font-family: monospace; letter-spacing: 1px; color: var(--text-dim); min-width: 74px; }
.preload-status.hot { color: var(--neon-green); }
.preload-status.loading { color: var(--accent-amber); }
.preload-btn {
  background: #0f2418;
  border: 1px solid var(--neon-dim);
  color: var(--neon-green);
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 1px;
  padding: 5px 12px;
  border-radius: 4px;
  cursor: pointer;
  font-family: monospace;
  transition: all 0.2s;
  white-space: nowrap;
}
.preload-btn:hover:not(:disabled) { background: var(--neon-green); color: #000; box-shadow: 0 0 12px var(--neon-green-glow); }
.preload-btn:disabled { opacity: 0.5; cursor: not-allowed; }

/* v2.15 HEAVY: request-state badge + live status line */
.tx-badge {
  font-size: 0.68rem;
  font-family: monospace;
  letter-spacing: 1px;
  padding: 3px 10px;
  border-radius: 10px;
  border: 1px solid var(--border-green);
  background: #0a110d;
  color: var(--text-dim);
  white-space: nowrap;
}
.tx-idle { color: var(--text-dim); }
.tx-sending { color: #7dd3fc; border-color: #0369a1; background: #082f49; animation: pulse-dot 1s infinite alternate; }
.tx-waiting { color: var(--accent-amber); border-color: #b45309; background: #451a03; animation: pulse-dot 1s infinite alternate; }
.tx-done { color: var(--neon-green); border-color: var(--neon-dim); background: #064e3b; }
.tx-error { color: #fca5a5; border-color: #b91c1c; background: #450a0a; }
.tx-status-line {
  font-size: 0.75rem;
  font-family: monospace;
  color: #7dd3fc;
  margin-bottom: 10px;
  min-height: 1.1em;
}
.tx-status-line.done { color: var(--neon-green); }
.tx-status-line.error { color: #fca5a5; }
.result-answer.error { color: #fecaca; }
.js-error-banner {
  display: none;
  background: #450a0a;
  border: 2px solid #b91c1c;
  color: #fecaca;
  font-family: monospace;
  font-size: 0.8rem;
  border-radius: 6px;
  padding: 10px 14px;
  margin-bottom: 16px;
}
.js-error-banner b { color: #fff; }

.query-input-wrap {
  display: flex;
  gap: 12px;
}

.queen-prompt-input {
  flex: 1;
  background: #050806;
  border: 1px solid var(--border-green);
  border-radius: 6px;
  padding: 14px 18px;
  color: #fff;
  font-size: 0.95rem;
  font-family: 'JetBrains Mono', monospace;
  outline: none;
  transition: all 0.2s;
}

.queen-prompt-input:focus {
  border-color: var(--neon-green);
  box-shadow: 0 0 15px rgba(0, 255, 102, 0.25);
}

.queen-submit-btn {
  background: linear-gradient(135deg, #059669 0%, #00ff66 100%);
  color: #041409;
  border: none;
  border-radius: 6px;
  padding: 12px 24px;
  font-size: 0.9rem;
  font-weight: 800;
  letter-spacing: 1px;
  text-transform: uppercase;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  transition: all 0.2s;
  box-shadow: 0 0 15px var(--neon-green-glow);
}

.queen-submit-btn:hover:not(:disabled) {
  transform: translateY(-2px);
  box-shadow: 0 0 25px rgba(0,255,102,0.6);
}

.queen-submit-btn:disabled { opacity: 0.5; cursor: not-allowed; }

/* Query Results Box */
.query-result-box {
  display: none;
  margin-top: 16px;
  background: #060b08;
  border: 1px solid #163622;
  border-radius: 6px;
  padding: 16px;
}

.result-answer {
  color: #d1fae5;
  font-size: 0.9rem;
  line-height: 1.6;
  white-space: pre-wrap;
  margin-bottom: 14px;
}

.sources-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid #163622;
}

.source-tag {
  background: #0f2418;
  border: 1px solid var(--border-green);
  color: var(--neon-green);
  font-size: 0.75rem;
  padding: 3px 8px;
  border-radius: 4px;
  font-family: monospace;
  cursor: pointer;
}
.source-tag:hover { background: var(--neon-dim); color: #000; }

.export-obsidian-btn {
  margin-left: auto;
  background: #1e3a29;
  color: #fff;
  border: 1px solid var(--neon-dim);
  padding: 4px 10px;
  border-radius: 4px;
  font-size: 0.75rem;
  cursor: pointer;
  font-family: monospace;
}
.export-obsidian-btn:hover { background: var(--neon-green); color: #000; }

/* Control Bar (Inbox Assimilation) */
.control-bar {
  background: var(--bg-card);
  border: 1px solid var(--border-green);
  border-radius: 8px;
  padding: 16px 20px;
  margin-bottom: 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
}

.control-info h3 { font-size: 1.05rem; color: #fff; margin-bottom: 2px; }
.control-info p { font-size: 0.8rem; color: var(--text-dim); font-family: monospace; }

.assimilate-btn {
  background: #11261b;
  color: var(--neon-green);
  border: 1px solid var(--neon-dim);
  border-radius: 6px;
  padding: 12px 24px;
  font-size: 0.9rem;
  font-weight: 800;
  letter-spacing: 1px;
  text-transform: uppercase;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  transition: all 0.2s ease;
}

.assimilate-btn:hover:not(:disabled) {
  background: var(--neon-dim);
  color: #000;
  box-shadow: 0 0 20px var(--neon-green-glow);
}
.assimilate-btn:disabled { opacity: 0.5; cursor: not-allowed; }

/* Swarm Pipeline */
.swarm-pipeline { display: flex; gap: 8px; margin-bottom: 16px; overflow-x: auto; padding-bottom: 4px; }
.pipeline-node { flex: 1; min-width: 110px; background: #09120c; border: 1px solid var(--border-green); border-radius: 6px; padding: 10px; text-align: center; transition: all 0.3s ease; }
.pipeline-node.active { border-color: var(--neon-green); box-shadow: 0 0 15px var(--neon-green-glow); background: #0e2015; }
.node-role { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1px; color: var(--text-dim); }
.node-model { font-size: 0.75rem; color: #fff; font-weight: bold; margin-top: 4px; font-family: monospace; }

/* Grid 2-Column */
.dashboard-grid { display: grid; grid-template-columns: 1.15fr 0.85fr; gap: 24px; }
@media (max-width: 960px) { .dashboard-grid { grid-template-columns: 1fr; } }

.panel { background: var(--bg-card); border: 1px solid var(--border-green); border-radius: 8px; overflow: hidden; display: flex; flex-direction: column; }
.panel-header { background: #111a14; padding: 14px 18px; border-bottom: 1px solid var(--border-green); display: flex; align-items: center; justify-content: space-between; }
.panel-header h3 { font-size: 0.88rem; text-transform: uppercase; letter-spacing: 1.5px; color: #fff; display: flex; align-items: center; gap: 8px; }
.panel-body { padding: 16px; flex: 1; }

.terminal-feed { background: #050806; border: 1px solid #10261a; border-radius: 6px; height: 380px; overflow-y: auto; padding: 12px; font-family: 'JetBrains Mono', monospace; font-size: 0.8rem; color: #a7f3d0; }
.log-entry { margin-bottom: 6px; line-height: 1.4; word-break: break-word; }
.log-time { color: #6ee7b7; opacity: 0.7; margin-right: 8px; }
.log-caste { font-weight: bold; padding: 2px 5px; border-radius: 3px; font-size: 0.7rem; margin-right: 6px; }
.caste-scout { background: #0369a1; color: #bae6fd; }
.caste-drone { background: #047857; color: #a7f3d0; }
.caste-adversary { background: #b45309; color: #fde68a; }
.caste-queen { background: #581c87; color: #f3e8ff; border: 1px solid #a855f7; }
.caste-system { background: #374151; color: #f3f4f6; }
.caste-index { background: #0f766e; color: #ccfbf1; }
.caste-query { background: #1e1b4b; color: #c7d2fe; border: 1px solid #6366f1; }

.studies-list { display: flex; flex-direction: column; gap: 10px; max-height: 420px; overflow-y: auto; }
.study-card { background: #0a110d; border: 1px solid #142a1d; border-radius: 6px; padding: 12px; cursor: pointer; transition: all 0.2s ease; }
.study-card:hover { border-color: var(--neon-dim); background: #0f1c14; }
.study-card-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
.study-title { font-size: 0.88rem; font-weight: 600; color: #f1f5f9; }
.study-badge { font-size: 0.68rem; padding: 2px 8px; border-radius: 10px; font-family: monospace; }
.badge-assimilated { background: #064e3b; color: #34d399; border: 1px solid #059669; }
.badge-pending { background: #451a03; color: #fbbf24; border: 1px solid #d97706; }
.study-preview { font-size: 0.78rem; color: #94a3b8; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }

/* Modal */
.modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0,0,0,0.85); backdrop-filter: blur(5px); z-index: 1000; align-items: center; justify-content: center; }
.modal-content { background: #0c1510; border: 2px solid var(--neon-dim); border-radius: 8px; width: 90%; max-width: 820px; max-height: 85vh; display: flex; flex-direction: column; box-shadow: 0 0 35px rgba(0,255,102,0.3); }
.modal-header { padding: 16px 20px; border-bottom: 1px solid var(--border-green); display: flex; align-items: center; justify-content: space-between; }
.modal-body { padding: 20px; overflow-y: auto; font-family: 'JetBrains Mono', monospace; font-size: 0.85rem; white-space: pre-wrap; color: #d1fae5; background: #060b08; }
.close-btn { background: transparent; border: none; color: var(--text-dim); font-size: 1.5rem; cursor: pointer; }
.close-btn:hover { color: #fff; }
</style>
</head>
<body>

<header>
  <div class="brand-left">
    <img src="/static/PATL.png" alt="PATL Logo" class="brand-logo-patl">
    <div class="brand-text">
      <h1>BORG QUEEN <span>CONTROL</span> <span style="font-size:0.7rem; background:#064e3b; color:#34d399; padding:2px 8px; border-radius:10px; border:1px solid #059669; font-weight:normal; letter-spacing:1px;">v2.15 HEAVY</span></h1>
      <p>Pickett Applied Technologies Laboratories // Swarm Ops</p>
    </div>
  </div>

  <div class="header-status-pills">
    <a href="/api/download_zip" class="download-btn">
      <span>💾</span> DOWNLOAD BORG_QUEEN.ZIP
    </a>
    <div class="pill">
      <div id="ollama-dot" class="pill-dot"></div>
      <span id="ollama-text">OLLAMA ENGINE</span>
    </div>
    <div class="pill">
      <span>RYZEN 9 // 8GB VRAM</span>
    </div>
    <img src="/static/Borg_Queen.png" alt="Borg Queen" class="brand-logo-queen">
  </div>
</header>

<div class="container">
  <div class="js-error-banner" id="js-error-banner"><b>⚠ BROWSER SCRIPT ERROR:</b> <span id="js-error-text"></span> <span style="opacity:0.7">(Press Ctrl+Shift+R to hard-refresh; if it persists, report this text.)</span></div>
  <!-- Telemetry Grid -->
  <div class="telemetry-grid">
    <div class="telemetry-card">
      <div class="telemetry-header">
        <span class="telemetry-label">Vault</span>
        <button class="reindex-mini-btn" id="btn-reindex" onclick="triggerReindex()">🔄 RE-INDEX</button>
      </div>
      <div class="telemetry-val" id="val-notes">5,161</div>
      <div class="telemetry-sub" id="vault-sub-label">/wiki (SQLite FTS5)</div>
    </div>
    <div class="telemetry-card">
      <div class="telemetry-header">
        <span class="telemetry-label">Inbox</span>
      </div>
      <div class="telemetry-val" id="val-pending" style="color: #fbbf24;">--</div>
      <div class="telemetry-sub">/inbox (Studies Pending)</div>
    </div>
    <div class="telemetry-card">
      <div class="telemetry-header">
        <span class="telemetry-label">Assimilated</span>
      </div>
      <div class="telemetry-val" id="val-assimilated">0</div>
      <div class="telemetry-sub">Enriched with [[wikilinks]]</div>
    </div>
    <div class="telemetry-card">
      <div class="telemetry-header">
        <span class="telemetry-label">Chamber</span>
      </div>
      <div class="telemetry-val" id="val-chamber" style="color: #a855f7;">64 GB</div>
      <div class="telemetry-sub" id="queen-status">Synthesis Ready</div>
    </div>
    <div class="telemetry-card hw">
      <div class="telemetry-header"><span class="telemetry-label">CPU</span></div>
      <div class="telemetry-val" id="val-cpu">--%</div>
      <div class="telemetry-sub" id="sub-cpu">load -- / cores</div>
      <div class="hw-bar" id="bar-cpu"><span></span></div>
    </div>
    <div class="telemetry-card hw">
      <div class="telemetry-header"><span class="telemetry-label">GPU</span></div>
      <div class="telemetry-val" id="val-gpu">--%</div>
      <div class="telemetry-sub" id="sub-gpu">VRAM --</div>
      <div class="hw-bar" id="bar-gpu"><span></span></div>
    </div>
    <div class="telemetry-card hw">
      <div class="telemetry-header"><span class="telemetry-label">Memory</span></div>
      <div class="telemetry-val" id="val-ram">-- GB</div>
      <div class="telemetry-sub" id="sub-ram">of -- GB</div>
      <div class="hw-bar" id="bar-ram"><span></span></div>
    </div>
  </div>

  <!-- COMMUNE WITH THE BORG QUEEN (PROMPT BOX) -->
  <div class="queen-query-panel">
    <div class="query-header">
      <h2><span>👑</span> COMMUNE WITH THE BORG QUEEN <span>// VAULT INTELLIGENCE</span> <span id="tx-badge" class="tx-badge tx-idle">TX: IDLE</span></h2>
      <div style="display: flex; align-items: center;">
      <div class="model-picker-wrap">
        <span>REASONING CORE:</span>
        <select id="query-model-select" class="model-picker-select" onchange="onModelChange()">
          <option value="cogito:32b" selected>cogito:32b (Balanced Queen)</option>
          <option value="cogito:14b-16k">cogito:14b-16k (Fast)</option>
          <option value="cogito:70b">cogito:70b (Deep RAM, single-pass)</option>
          <option value="borg_queen:72b">borg_queen:72b (Deep RAM, single-pass)</option>
          <option value="olmo-3:7b-think">olmo-3:7b-think (VRAM drone)</option>
          <option value="zarigata/unfiltered-llama3:latest">zarigata/unfiltered-llama3 (Adversary)</option>
        </select>
      </div>
      <div class="preload-wrap" title="Pin the reasoning core HOT in VRAM/RAM so TRANSMIT answers instantly">
        <div id="preload-dot" class="preload-dot"></div>
        <span id="preload-status" class="preload-status">COLD</span>
        <button class="preload-btn" id="btn-preload" onclick="preloadModel(false)">🔥 PRELOAD 32B</button>
      </div>
      </div>
    </div>

    <div class="query-input-wrap">
      <input type="text" id="queen-prompt-input" class="queen-prompt-input" 
             placeholder="Ask anything across your 5,161 vault notes (e.g. 'Synthesize our findings on high entropy alloy wear resistance')..."
             onkeydown="if(event.key==='Enter') executeHiveQuery()">
      <button type="button" class="queen-submit-btn" id="btn-queen-query">
        <span>⚡ TRANSMIT</span>
      </button>
    </div>

    <!-- Live Query Response Area -->
    <div class="query-result-box" id="query-result-box">
      <div style="font-size: 0.75rem; text-transform: uppercase; color: var(--neon-green); margin-bottom: 8px; font-family: monospace;">
        <span>✨ SYNTHESIS FROM 5,161 INDEXED VAULT NOTES:</span>
      </div>
      <div class="tx-status-line" id="tx-status-line"></div>
      <div class="result-answer" id="result-answer"></div>
      <div class="sources-bar" id="sources-bar">
        <span style="font-size: 0.72rem; color: var(--text-dim); text-transform: uppercase;">Sources:</span>
        <div id="sources-tags-container" style="display: flex; gap: 6px; flex-wrap: wrap;"></div>
        <button class="export-obsidian-btn" onclick="exportQueryToObsidian()">💾 Export as Obsidian Note</button>
      </div>
    </div>
  </div>

  <!-- Inbox Control Bar -->
  <div class="control-bar">
    <div class="control-info">
      <h3>Autonomous Daily Intake Swarm</h3>
      <p id="inbox-path-display">Inbox: ~/Documents/PATL/Lab_Second_brain/inbox</p>
    </div>
    <button class="assimilate-btn" id="btn-assimilate" onclick="triggerAssimilation()">
      <span id="btn-icon">⚡</span>
      <span id="btn-label">ASSIMILATE INBOX STUDIES</span>
    </button>
  </div>

  <!-- Swarm Castes Pipeline Visualizer -->
  <div class="swarm-pipeline">
    <div class="pipeline-node" id="node-scout">
      <div class="node-role">1. SCOUT DRONE</div>
      <div class="node-model">qwen2.5vl:7b</div>
      <div style="font-size: 0.65rem; color: #10b981;">100% VRAM (~80 t/s)</div>
    </div>
    <div class="pipeline-node" id="node-index">
      <div class="node-role">2. GRAPH HUNTER</div>
      <div class="node-model">SQLite FTS5</div>
      <div style="font-size: 0.65rem; color: #06b6d4;">5,161 Notes (3ms)</div>
    </div>
    <div class="pipeline-node" id="node-drone">
      <div class="node-role">3. CROSS-LINKER</div>
      <div class="node-model">olmo-3:7b-think</div>
      <div style="font-size: 0.65rem; color: #10b981;">100% VRAM (~45 t/s)</div>
    </div>
    <div class="pipeline-node" id="node-adversary">
      <div class="node-role">4. ADVERSARY</div>
      <div class="node-model">unfiltered-llama3</div>
      <div style="font-size: 0.65rem; color: #f59e0b;">Red-Team Bias (~35 t/s)</div>
    </div>
    <div class="pipeline-node" id="node-queen">
      <div class="node-role">5. BORG QUEEN</div>
      <div class="node-model">cogito:32b / 72b</div>
      <div style="font-size: 0.65rem; color: #a855f7;">64GB RAM Briefing</div>
    </div>
  </div>

  <!-- Dashboard 2-Column -->
  <div class="dashboard-grid">
    <div class="panel">
      <div class="panel-header">
        <h3><span>🟢</span> HIVE TELEMETRY & STIGMERGIC LOG</h3>
        <span id="live-step-badge" style="font-size: 0.75rem; font-family: monospace; color: var(--neon-green);">STATUS: IDLE</span>
      </div>
      <div class="panel-body">
        <div class="terminal-feed" id="terminal-feed">
          <div class="log-entry">
            <span class="log-time">[INIT]</span>
            <span class="log-caste caste-queen">QUEEN</span>
            Borg Queen Control initialized. Pickett Applied Tech Lab inbox monitor ready.
          </div>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <h3><span>📥</span> INBOX STUDIES FEED</h3>
        <button onclick="refreshStudies()" style="background: none; border: 1px solid var(--border-green); color: var(--neon-green); font-size: 0.7rem; padding: 4px 8px; border-radius: 4px; cursor: pointer;">REFRESH</button>
      </div>
      <div class="panel-body">
        <div class="studies-list" id="studies-list">
          <div style="color: var(--text-dim); font-size: 0.8rem; text-align: center; padding: 20px;">Scanning inbox directory...</div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Modal for Viewing Markdown Note -->
<div class="modal-overlay" id="note-modal" onclick="closeModal(event)">
  <div class="modal-content" onclick="event.stopPropagation()">
    <div class="modal-header">
      <h3 id="modal-title" style="color: #fff; font-size: 1rem;">Note Inspector</h3>
      <button class="close-btn" onclick="closeModal()">×</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<script src="/static/queen.js"></script>
<script>
(function () {
  function el(id) { return document.getElementById(id); }
  function go() {
    if (typeof window.executeHiveQuery === 'function') window.executeHiveQuery();
    else alert('Queen UI script did not load. Hard-refresh (Ctrl+Shift+R).');
  }
  var btn = el('btn-queen-query');
  var input = el('queen-prompt-input');
  if (btn) {
    btn.setAttribute('type', 'button');
    btn.addEventListener('click', function (e) { e.preventDefault(); go(); });
  }
  if (input) {
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); go(); }
    });
  }
})();
</script>
</body>
</html>
"""

def create_app():
    app = web.Application()
    app.on_startup.append(on_app_startup)
    app.router.add_get('/', handle_index)
    app.router.add_get('/favicon.ico', handle_favicon)
    app.router.add_get('/api/status', handle_status)
    app.router.add_post('/api/reindex', handle_reindex)
    app.router.add_post('/api/assimilate', handle_trigger)
    app.router.add_post('/api/query', handle_query_hive)
    app.router.add_post('/api/preload', handle_preload)
    app.router.add_get('/api/diag', handle_diag)
    app.router.add_get('/api/ps', handle_ps)
    app.router.add_post('/api/save_note', handle_save_to_obsidian)
    app.router.add_get('/api/studies', handle_get_studies)
    app.router.add_get('/api/study', handle_get_study_content)
    app.router.add_get('/api/download_zip', handle_download_zip)
    app.router.add_static('/static/', path=str(STATIC_DIR), name='static')
    return app

def main():
    parser = argparse.ArgumentParser(description="Borg Queen Control Swarm Server")
    parser.add_argument("--port", "-p", type=int, default=None, help="Port to listen on (default: 8082 or BORG_QUEEN_PORT env)")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind to (default: 0.0.0.0)")
    parser.add_argument("--index", action="store_true", help="Run vault indexer and exit immediately")
    parser.add_argument("--preload", nargs="?", const="cogito:32b", default=None, metavar="MODEL",
                        help="Preload a reasoning core into VRAM/RAM and exit (default: cogito:32b)")
    args = parser.parse_args()

    if args.preload:
        model = args.preload
        print(f"\n[*] Preloading reasoning core into VRAM/RAM: {model}")
        print(f"[*] Ollama endpoint: {OLLAMA_BASE_URL} (expect GPU spike + ~60-90s cold for 14B)\n")
        async def _cli_preload():
            ok = await swarm.check_ollama()
            if not ok:
                print("❌ Ollama is offline at http://localhost:11434 — start it with `ollama serve` first.")
                sys.exit(1)
            res = await swarm.preload_model(model, background=False)
            print(f"\nPreload result: {res}\n")
        asyncio.run(_cli_preload())
        sys.exit(0)

    if args.index:
        print(f"\n[*] Scanning and indexing Obsidian Wiki Vault: {v_path}")
        start = time.time()
        count = swarm.sync_index(force=True)
        elapsed = time.time() - start
        print(f"✅ Indexed {count} notes into SQLite FTS5 in {elapsed:.2f} seconds!")
        print(f"📁 Database: {v_path / '.hive_index.sqlite'}\n")
        sys.exit(0)

    env_port = os.environ.get("BORG_QUEEN_PORT") or os.environ.get("PORT")
    requested_port = args.port or (int(env_port) if env_port else 8082)

    actual_port = find_available_port(requested_port)
    if actual_port != requested_port:
        print(f"\n⚠️  [PORT BUSY] Requested port {requested_port} is already in use!")
        print(f"🔄  [AUTO-SWITCH] Bound successfully to next available port: {actual_port}\n")
    else:
        print(f"\n✅  [PORT READY] Binding to {args.host}:{actual_port}\n")

    app = create_app()
    web.run_app(app, host=args.host, port=actual_port, reuse_address=True)

if __name__ == '__main__':
    main()
