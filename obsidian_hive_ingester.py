"""
Obsidian Second Brain - AI Hive Mind Ingestion & Synthesis Engine
Pickett Applied Technologies Laboratories (PATL)
=================================================================
Target vault: ~/Documents/PATL/Lab_Second_brain/wiki (override with BORG_VAULT)
Studies inbox: ~/Documents/PATL/Lab_Second_brain/inbox (override with BORG_INBOX)
"""

import asyncio
import glob
import json
import os
import re
import sqlite3
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

VAULT_PATH = Path(os.environ.get("BORG_VAULT", "")).expanduser() if os.environ.get("BORG_VAULT") else Path.home() / "Documents/PATL/Lab_Second_brain/wiki"
INBOX_PATH = Path(os.environ.get("BORG_INBOX", "")).expanduser() if os.environ.get("BORG_INBOX") else Path.home() / "Documents/PATL/Lab_Second_brain/inbox"
INDEX_DB_PATH = VAULT_PATH / ".hive_index.sqlite"
STATE_FILE = VAULT_PATH / ".hive_processed.json"
OLLAMA_URL = "http://localhost:11434/api/generate"

MODELS = {
    "scout": "qwen2.5vl:7b",
    "cross_linker": "olmo-3:7b-think",
    "adversary": "zarigata/unfiltered-llama3:latest",
    "queen": "cogito:32b"
}

class VaultIndex:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(str(db_path))
        self._init_db()

    def _init_db(self):
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

    def sync_vault(self, vault_dir: Path):
        if not vault_dir.exists():
            return
        cursor = self.conn.cursor()
        cursor.execute("SELECT path, mtime FROM meta")
        indexed_files = dict(cursor.fetchall())

        all_mds = list(vault_dir.rglob("*.md"))
        to_index = []

        for md in all_mds:
            str_path = str(md.relative_to(vault_dir))
            mtime = md.stat().st_mtime
            if str_path not in indexed_files or indexed_files[str_path] < mtime:
                try:
                    content = md.read_text(encoding="utf-8", errors="ignore")
                    title = md.stem
                    to_index.append((str_path, title, content, mtime))
                except Exception:
                    continue

        if to_index:
            print(f"[*] Indexing {len(to_index)} new/modified notes into local FTS5 engine...")
            with self.conn:
                for str_path, title, content, mtime in to_index:
                    self.conn.execute("DELETE FROM notes_fts WHERE path = ?", (str_path,))
                    self.conn.execute("INSERT INTO notes_fts(path, title, content) VALUES(?, ?, ?)", (str_path, title, content))
                    self.conn.execute("INSERT OR REPLACE INTO meta(path, mtime) VALUES(?, ?)", (str_path, mtime))
            print("[+] Vault index updated.")

    def search_similar(self, query: str, limit: int = 5, exclude_path: str = "") -> List[Tuple[str, str, float]]:
        words = re.findall(r'\b[a-zA-Z]{3,}\b', query)
        if not words:
            return []
        fts_query = " OR ".join(words[:8])
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                SELECT path, title, bm25(notes_fts) as rank
                FROM notes_fts
                WHERE notes_fts MATCH ? AND path != ?
                ORDER BY rank
                LIMIT ?
            """, (fts_query, exclude_path, limit))
            return cursor.fetchall()
        except sqlite3.OperationalError:
            return []

def call_ollama(model: str, system: str, prompt: str, max_tokens: int = 150, temperature: float = 0.3) -> str:
    payload = {
        "model": model,
        "prompt": f"<system>\n{system}\n</system>\n\n<input>\n{prompt}\n</input>\n\nResponse:",
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens}
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("response", "").strip()
    except Exception as e:
        return f"[Inference error: {e}]"

class ObsidianHiveIngester:
    def __init__(self, vault_path: Path, inbox_path: Path):
        self.vault_path = vault_path
        self.inbox_path = inbox_path
        self.index = VaultIndex(INDEX_DB_PATH)
        self.processed_records = self._load_state()

    def _load_state(self) -> set:
        if STATE_FILE.exists():
            try:
                return set(json.loads(STATE_FILE.read_text()))
            except Exception:
                return set()
        return set()

    def _save_state(self):
        STATE_FILE.write_text(json.dumps(list(self.processed_records), indent=2))

    def process_study(self, study_path: Path) -> Dict[str, Any]:
        content = study_path.read_text(encoding="utf-8", errors="ignore")
        rel_path = str(study_path.name)
        print(f"\n🐝 [HIVE PROCESSING] {study_path.name}")

        scout_sys = "You are a research triage scout. Extract: (1) Core thesis in 1 sentence. (2) 5 domain keywords for database search. Format: JSON with keys 'thesis' and 'keywords'."
        scout_raw = call_ollama(MODELS["scout"], scout_sys, content[:2500], max_tokens=120, temperature=0.2)
        
        search_terms = " ".join(re.findall(r'\b[a-zA-Z]{3,}\b', scout_raw))
        matches = self.index.search_similar(search_terms, limit=4)
        related_titles = [m[1] for m in matches]
        print(f"   ↳ Cross-referenced with 5k wiki notes: found {len(related_titles)} related: {related_titles}")

        crosslink_prompt = f"New Study:\n{scout_raw}\n\nRelated Vault Notes:\n{', '.join(related_titles)}\nExplain how this study connects to these notes and generate Obsidian [[wikilinks]]."
        crosslink_res = call_ollama(MODELS["cross_linker"], "You are a second-brain knowledge graph architect. Generate concise bullet points with [[wikilinks]].", crosslink_prompt, max_tokens=150)

        adversary_prompt = f"Study Summary: {scout_raw}\nIdentify 1 major potential caveat or methodological limitation."
        adversary_res = call_ollama(MODELS["adversary"], "You are a skeptical peer reviewer. Find the weakness in 1-2 sharp sentences.", adversary_prompt, max_tokens=100)

        hive_block = f"""

---
## 🐝 Hive Mind Knowledge Fabric
**Core Thesis**: {scout_raw}

### 🔗 Cross-Vault Connections
{crosslink_res}

### ⚠️ Epistemic & Methodological Caveats
{adversary_res}
*Processed by PATL Local AI Swarm: {datetime.now().strftime('%Y-%m-%d %H:%M')}*
"""
        with open(study_path, "a", encoding="utf-8") as f:
            f.write(hive_block)

        self.processed_records.add(rel_path)
        self._save_state()

        return {
            "title": study_path.stem,
            "path": rel_path,
            "thesis": scout_raw,
            "related": related_titles
        }

    def run_inbox_batch(self):
        self.index.sync_vault(self.vault_path)
        if not self.inbox_path.exists():
            self.inbox_path.mkdir(parents=True, exist_ok=True)
            print(f"[!] Created inbox at: {self.inbox_path}")
            return

        all_studies = list(self.inbox_path.glob("*.md"))
        unprocessed = [s for s in all_studies if s.name not in self.processed_records]

        print(f"[*] Found {len(unprocessed)} new studies in inbox awaiting Hive assimilation.")
        daily_summaries = []
        for i, study in enumerate(unprocessed, 1):
            print(f"[{i}/{len(unprocessed)}]", end=" ")
            res = self.process_study(study)
            daily_summaries.append(res)

        if daily_summaries:
            self.generate_queen_daily_briefing(daily_summaries)

    def generate_queen_daily_briefing(self, summaries: List[Dict[str, Any]]):
        print("\n" + "="*70)
        print("👑 INVOKING THE BORG QUEEN FOR DAILY MASTER SYNTHESIS...")
        print("="*70)

        prompt_data = "\n".join([f"- [[{s['title']}]]: {s['thesis']} (Linked to: {s['related']})" for s in summaries[:30]])
        queen_prompt = f"Today's {len(summaries)} newly ingested studies from inbox:\n{prompt_data}\n\nSynthesize the overarching paradigm shifts, new clusters, and top 3 must-read papers for Bob's second brain."

        queen_res = call_ollama(
            MODELS["queen"],
            "You are the Borg Queen / Master Knowledge Synthesizer. Write an authoritative, beautifully structured Obsidian summary note.",
            queen_prompt,
            max_tokens=400,
            temperature=0.2
        )

        today_str = datetime.now().strftime("%Y-%m-%d")
        briefing_path = self.vault_path / f"Daily_Hive_Synthesis_{today_str}.md"
        briefing_content = f"""---
tags: [hive-mind, daily-synthesis, second-brain, research-intake, patl]
date: {today_str}
papers_processed: {len(summaries)}
source: inbox
---
# 🧠 PATL Daily Hive Mind Synthesis — {today_str}

## 🌐 The Queen's Strategic Assessment
{queen_res}

## 📥 Ingested Studies From Inbox
""" + "\n".join([f"- [[{s['title']}]] — *Cross-linked with:* {', '.join([f'[[{r}]]' for r in s['related']])}" for s in summaries])

        briefing_path.write_text(briefing_content, encoding="utf-8")
        print(f"✨ Master Briefing written to: {briefing_path}")

if __name__ == "__main__":
    ingester = ObsidianHiveIngester(VAULT_PATH, INBOX_PATH)
    ingester.run_inbox_batch()
