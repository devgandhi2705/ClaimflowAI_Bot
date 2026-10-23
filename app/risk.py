"""Risk scoring and routing decision."""
from typing import List, Optional

from app.config import load_policy
from app.models import Decision, Flag, ReceiptData, Status


def decide(data: ReceiptData, flags: List[Flag], fuzzy_dup: Optional[dict] = None,
           policy: Optional[dict] = None) -> Decision:
    p = policy or load_policy()
    w, r = p["risk_weights"], p["routing"]
    flags = list(flags)

    if fuzzy_dup:
        flags.append(Flag(code="SUSPECTED_DUPLICATE", severity="warn", points=w["suspected_duplicate"],
                          message=f"Looks like {fuzzy_dup['claim_id']} ({fuzzy_dup['reason']}, "
                                  f"{fuzzy_dup['similarity']}% match)"))

    confidence_points = round((1 - data.confidence) * w["low_confidence_factor"])
    if confidence_points:
        flags.append(Flag(code="AI_CONFIDENCE", severity="info", points=confidence_points,
                          message=f"AI confidence {data.confidence:.0%}"))

    score = min(100, sum(f.points for f in flags))
    hard = [f for f in flags if f.severity == "hard"]
    amount = data.total_amount or 0

    if hard:
        return Decision(status=Status.AUTO_REJECTED, risk_score=max(score, 90), flags=flags,
                        reason="; ".join(f.message for f in hard))
    if score >= r["auto_reject_min_risk"]:
        return Decision(status=Status.AUTO_REJECTED, risk_score=score, flags=flags,
                        reason=f"Risk score {score} is above the auto-reject threshold ({r['auto_reject_min_risk']})")
    if score < r["auto_approve_max_risk"] and amount <= p["auto_approve_limit"]:
        return Decision(status=Status.AUTO_APPROVED, risk_score=score, flags=flags,
                        reason=f"Low risk ({score}) and within the auto-approval limit (₹{p['auto_approve_limit']:,})")
    why = []
    if amount > p["auto_approve_limit"]:
        why.append(f"amount above auto-approval limit (₹{p['auto_approve_limit']:,})")
    if score >= r["auto_approve_max_risk"]:
        why.append(f"risk score {score}")
    return Decision(status=Status.PENDING_APPROVAL, risk_score=score, flags=flags,
                    reason="Manager approval required: " + ", ".join(why))
