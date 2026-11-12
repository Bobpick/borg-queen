"""
Ollama Local Hive Mind Orchestrator
====================================
Tailored to orchestrate your local Ollama models into an asynchronous,
stigmergic Hive Mind.

Zero external pip dependencies: Uses Python standard library (asyncio, urllib, json).
"""

import asyncio
import json
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

OLLAMA_BASE_URL = "http://localhost:11434"

# ---------------------------------------------------------------------------
# HIVE ROSTER CONFIGURATION
# Modify this mapping depending on your available RAM / Unified Memory.
#
# Option A (Low VRAM / 16-32GB):
#   Scout: "qwen2.5vl:7b"
#   Drone: "olmo-3:7b-think"
#   Adversary: "zarigata/unfiltered-llama3:latest"
#   Queen / Synthesizer: "cogito:32b"
#
# Option B (High VRAM / 64-128GB Unified Memory):
#   Scout: "qwen2.5vl:7b"
#   Drone: "borg_drone:32b" (or "olmo-3:32b-think")
#   Adversary: "zarigata/unfiltered-llama3:latest"
#   Queen / Synthesizer: "borg_queen:72b" (or "qwen2.5:72b-think")
# ---------------------------------------------------------------------------

HIVE_CONFIG = {
    "scout": {
        "model": "qwen2.5vl:7b",
        "temperature": 0.8,
        "max_tokens": 220,
        "prompt_role": "You are the Hive Scout. Rapidly explore novel solution hypotheses and orthogonal problem angles. Keep output under 3 sentences."
    },
    "drone_executor": {
        "model": "olmo-3:7b-think",  # Swap to "borg_drone:32b" if >32GB RAM
        "temperature": 0.3,
        "max_tokens": 250,
        "prompt_role": "You are a Hive Drone. Test logic, provide concrete implementation steps and empirical verification. Keep output under 3 sentences."
    },
    "adversary": {
        "model": "zarigata/unfiltered-llama3:latest",
        "temperature": 0.7,
        "max_tokens": 220,
        "prompt_role": "You are the Hive Adversary. Ruthlessly find flaws, edge cases, vulnerabilities, and false assumptions in proposed hypotheses. Refute cleanly."
    },
    "queen": {
        "model": "cogito:70b",  # Swap to "borg_queen:72b" if you have 64GB+ unified memory
        "temperature": 0.2,
        "max_tokens": 450,
        "prompt_role": "You are the Borg Queen / Hive Synthesizer. Analyze active corroborated hypotheses and synthesize them into definitive consensus facts."
    }
}

@dataclass
class PheromoneSignal:
    id: str
    author: str
    role: str
    content: str
    signal_type: str  # "hypothesis", "refutation", "endorsement", "consensus"
    target_id: Optional[str] = None
    confidence: float = 0.5
    endorsements: int = 1
    refutations: int = 0
    timestamp: float = field(default_factory=time.time)
    decay_rate: float = 0.05

    @property
    def strength(self) -> float:
        age = time.time() - self.timestamp
        decay = math.exp(-self.decay_rate * age)
        ratio = self.endorsements / (self.endorsements + self.refutations)
        return self.confidence * ratio * decay

class Blackboard:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.signals: Dict[str, PheromoneSignal] = {}
        self.consensus: List[str] = []
        self.objective: str = ""

    async def post(self, signal: PheromoneSignal):
        async with self.lock:
            self.signals[signal.id] = signal
            print(f"\n[{signal.role.upper()} | {signal.author}] -> {signal.signal_type.upper()}")
            print(f"   \"{signal.content.strip()}\" (Strength: {signal.strength:.2f})")

    async def endorse(self, signal_id: str, voter: str):
        async with self.lock:
            if signal_id in self.signals:
                self.signals[signal_id].endorsements += 1
                self.signals[signal_id].confidence = min(1.0, self.signals[signal_id].confidence + 0.15)
                print(f"  ↳ [ENDORSED by {voter}] on {signal_id} -> New Strength: {self.signals[signal_id].strength:.2f}")

    async def refute(self, signal_id: str, voter: str, reason: str):
        async with self.lock:
            if signal_id in self.signals:
                self.signals[signal_id].refutations += 1
                self.signals[signal_id].confidence = max(0.05, self.signals[signal_id].confidence - 0.25)
                print(f"  ↳ [REFUTED by {voter}] on {signal_id}: \"{reason[:60]}...\" -> New Strength: {self.signals[signal_id].strength:.2f}")

    async def active_signals(self, threshold: float = 0.15) -> List[PheromoneSignal]:
        async with self.lock:
            return [s for s in self.signals.values() if s.strength >= threshold]

async def call_ollama(model: str, system_prompt: str, user_prompt: str, temperature: float = 0.7, max_tokens: int = 150) -> str:
    """Invokes local Ollama via native HTTP request in a thread pool executor."""
    url = f"{OLLAMA_BASE_URL}/api/generate"
    payload = {
        "model": model,
        "prompt": f"{system_prompt}\n\nContext & Task:\n{user_prompt}\n\nResponse:",
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens
        }
    }

    def _sync_request():
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("response", "").strip()
        except urllib.error.URLError as e:
            return f"[Ollama Connection Error: Is Ollama running on localhost:11434? {e}]"

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_request)

async def scout_loop(board: Blackboard, cycles: int = 2):
    cfg = HIVE_CONFIG["scout"]
    for i in range(cycles):
        active = await board.active_signals()
        summary = f"Objective: {board.objective}\nCurrent blackboard items: {len(active)}"
        res = await call_ollama(cfg["model"], cfg["prompt_role"], summary, cfg["temperature"], cfg["max_tokens"])
        
        signal = PheromoneSignal(
            id=f"scout-{i}",
            author="Scout",
            role="Scout",
            content=res,
            signal_type="hypothesis",
            confidence=0.75
        )
        await board.post(signal)
        await asyncio.sleep(1)

async def adversary_loop(board: Blackboard, cycles: int = 2):
    cfg = HIVE_CONFIG["adversary"]
    for i in range(cycles):
        await asyncio.sleep(1.5)
        active = await board.active_signals()
        hypotheses = [s for s in active if s.signal_type == "hypothesis"]
        if hypotheses:
            target = max(hypotheses, key=lambda s: s.strength)
            context = f"Hypothesis to critique: {target.content}"
            res = await call_ollama(cfg["model"], cfg["prompt_role"], context, cfg["temperature"], cfg["max_tokens"])
            await board.refute(target.id, "Adversary", res)

async def drone_loop(board: Blackboard, cycles: int = 2):
    cfg = HIVE_CONFIG["drone_executor"]
    for i in range(cycles):
        await asyncio.sleep(2.0)
        active = await board.active_signals()
        hypotheses = [s for s in active if s.signal_type == "hypothesis"]
        if hypotheses:
            target = min(hypotheses, key=lambda s: s.strength)  # Help evaluate challenged hypotheses
            context = f"Validate or test this claim with evidence: {target.content}"
            res = await call_ollama(cfg["model"], cfg["prompt_role"], context, cfg["temperature"], cfg["max_tokens"])
            await board.endorse(target.id, "Drone")
            drone_signal = PheromoneSignal(
                id=f"drone-{i}",
                author="Drone",
                role="Drone",
                content=res,
                signal_type="evidence",
                confidence=0.85
            )
            await board.post(drone_signal)

async def queen_loop(board: Blackboard):
    cfg = HIVE_CONFIG["queen"]
    await asyncio.sleep(4.0)  # Let drones and scouts establish signals
    active = await board.active_signals()
    signals_summary = "\n".join([f"- [{s.role}] ({s.strength:.2f} strength): {s.content}" for s in active])
    context = f"Goal: {board.objective}\n\nSignals from the swarm:\n{signals_summary}\n\nSynthesize into an authoritative consensus conclusion:"
    
    print("\n>>> QUEEN INGESTING SWARM PHEROMONES FOR FINAL SYNTHESIS...")
    consensus = await call_ollama(cfg["model"], cfg["prompt_role"], context, cfg["temperature"], cfg["max_tokens"])
    print(f"\n" + "="*70)
    print("✨ BORG QUEEN / SYNTHESIZER CONSENSUS REACHED ✨")
    print("="*70)
    print(consensus)
    print("="*70)

async def main():
    board = Blackboard()
    board.objective = "Formulate the optimal deployment architecture for a local AI agent swarm."
    
    print("="*70)
    print("STARTING LOCAL OLLAMA HIVE MIND")
    print(f"Scout:     {HIVE_CONFIG['scout']['model']}")
    print(f"Drone:     {HIVE_CONFIG['drone_executor']['model']}")
    print(f"Adversary: {HIVE_CONFIG['adversary']['model']}")
    print(f"Queen:     {HIVE_CONFIG['queen']['model']}")
    print("="*70)

    await asyncio.gather(
        scout_loop(board, cycles=2),
        adversary_loop(board, cycles=2),
        drone_loop(board, cycles=2),
        queen_loop(board)
    )

if __name__ == "__main__":
    asyncio.run(main())
