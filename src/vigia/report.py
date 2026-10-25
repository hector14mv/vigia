"""`vigia report`: the engine table, from the ledger alone.

Per builder model: PRs opened and merged, how often the gate's first round found a P1, gate rounds per merged
PR, minutes from PR to merge, parks. Per reviewer model: verdicts, P1s found, verdicts it failed to return. It is
how you see, with numbers rather than impressions, which engine to trust with which tier.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from .ledger import Ledger


def _ts(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(value or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def build(ledgers: dict[str, Ledger], days: int = 7, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    builders: dict[str, dict] = defaultdict(
        lambda: {
            "opened": 0,
            "merged": 0,
            "first_p1": 0,
            "gated": 0,
            "rounds": [],
            "minutes": [],
            "parked": 0,
            "tiers": set(),
        }
    )
    reviewers: dict[str, dict] = defaultdict(
        lambda: {"verdicts": 0, "p1": 0, "p2": 0, "unavailable": 0, "kinds": set()}
    )
    totals = {"merged": 0, "p1_gate": 0, "p1_review": 0, "parks": 0, "fix_rounds": 0, "updates": 0}
    for _repo, ledger in ledgers.items():
        every = ledger.rows()
        rows = [r for r in every if (_ts(r.get("ts")) or since) >= since]
        # A PR merged or parked in the window may have been opened, and gated, before it: its builder and its
        # rounds come from the whole ledger, while what is counted stays the window's.
        ever_opened = {r["pr"]: r for r in every if r["event"] == "pr_opened"}
        ever_gated = defaultdict(set)
        for r in every:
            if r["event"] == "verdict" and r.get("kind") == "gate" and r.get("verdict") != "UNAVAILABLE":
                ever_gated[r.get("pr")].add(r["sha"])
        opened = {r["pr"]: r for r in rows if r["event"] == "pr_opened"}
        verdicts = defaultdict(list)
        for r in rows:
            if r["event"] == "verdict":
                verdicts[r.get("pr")].append(r)
                who = reviewers[f"{r.get('engine')} · {r.get('model')}"]
                who["kinds"].add(r.get("kind"))
                if r.get("verdict") == "UNAVAILABLE":
                    who["unavailable"] += 1
                else:
                    who["verdicts"] += 1
                    who["p1"] += r.get("p1", 0)
                    who["p2"] += r.get("p2", 0)
                    totals["p1_gate" if r.get("kind") == "gate" else "p1_review"] += r.get("p1", 0)
            elif r["event"] == "parked":
                totals["parks"] += 1
                if r.get("pr") in ever_opened:
                    o = ever_opened[r["pr"]]
                    builders[f"{o['engine']} · {o['model']}"]["parked"] += 1
            elif r["event"] == "fix_started":
                totals["fix_rounds"] += 1
            elif r["event"] == "updated":
                totals["updates"] += 1
        for n, o in opened.items():
            b = builders[f"{o['engine']} · {o['model']}"]
            b["opened"] += 1
            b["tiers"].add(o.get("tier"))
            gates = [v for v in verdicts[n] if v.get("kind") == "gate" and v.get("verdict") != "UNAVAILABLE"]
            if gates:
                b["gated"] += 1
                b["first_p1"] += gates[0].get("p1", 0) > 0
        for r in rows:
            if r["event"] != "merged":
                continue
            totals["merged"] += 1
            o = ever_opened.get(r.get("pr"))
            if not o:
                continue
            b = builders[f"{o['engine']} · {o['model']}"]
            b["merged"] += 1
            b["tiers"].add(o.get("tier"))
            b["rounds"].append(len(ever_gated[r["pr"]]))
            start, end = _ts(o["ts"]), _ts(r["ts"])
            if start and end:
                b["minutes"].append((end - start).total_seconds() / 60)

    def median(values: list[float]) -> float | None:
        return round(statistics.median(values), 1) if values else None

    return {
        "days": days,
        "repos": sorted(ledgers),
        "totals": totals,
        "builders": [
            {
                "model": k,
                "tiers": sorted(t for t in v["tiers"] if t),
                "opened": v["opened"],
                "merged": v["merged"],
                "first_round_p1_rate": round(v["first_p1"] / v["gated"], 2) if v["gated"] else None,
                "gate_rounds_per_merge": round(sum(v["rounds"]) / len(v["rounds"]), 2)
                if v["rounds"]
                else None,
                "median_minutes_to_merge": median(v["minutes"]),
                "parked": v["parked"],
            }
            for k, v in sorted(builders.items())
        ],
        "reviewers": [
            {
                "model": k,
                "as": sorted(v["kinds"] - {None}),
                "verdicts": v["verdicts"],
                "p1": v["p1"],
                "p2": v["p2"],
                "unavailable": v["unavailable"],
            }
            for k, v in sorted(reviewers.items())
        ],
    }


def render(data: dict) -> str:
    t = data["totals"]
    totals = (
        f"{t['merged']} merged · {t['p1_gate']} P1 found by the gate, {t['p1_review']} by tier reviewers · "
        f"{t['fix_rounds']} fix rounds · {t['parks']} parks"
    )
    if t["updates"] > 0:
        totals += f" · {t['updates']} base updates"
    window = "last day" if data["days"] == 1 else f"last {data['days']} days"
    out = [
        f"vigia report · {window} · {', '.join(data['repos'])}",
        totals,
        "",
        "Builders            tiers     opened merged  first-round P1  gate rounds/merge  median min  parked",
    ]
    for b in data["builders"]:
        rate = "–" if b["first_round_p1_rate"] is None else f"{b['first_round_p1_rate']:.0%}"
        rounds = "–" if b["gate_rounds_per_merge"] is None else f"{b['gate_rounds_per_merge']:.2f}"
        minutes = "–" if b["median_minutes_to_merge"] is None else f"{b['median_minutes_to_merge']:.0f}"
        out.append(
            f"  {b['model']:<26} {','.join(b['tiers']):<9} {b['opened']:>4} {b['merged']:>6}  {rate:>14}  "
            f"{rounds:>17}  {minutes:>10}  {b['parked']:>6}"
        )
    out += ["", "Reviewers           as            verdicts   P1   P2  no verdict"]
    for r in data["reviewers"]:
        out.append(
            f"  {r['model']:<26} {','.join(r['as']):<12} {r['verdicts']:>8} {r['p1']:>4} {r['p2']:>4}  {r['unavailable']:>10}"
        )
    return "\n".join(out)


def show(ledgers: dict[str, Ledger], days: int = 7, as_json: bool = False) -> str:
    data = build(ledgers, days)
    return json.dumps(data, indent=2) if as_json else render(data)
