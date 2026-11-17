"""Provider-counted answer context assembled from one EvidencePacket."""

import json
import re
from typing import Any

from austra.skills import load_skill

MODEL = "claude-sonnet-5"
INPUT_TARGET = 12_000
INPUT_HARD_CAP = 16_000
OUTPUT_RESERVE = 3_000
NUMERIC = re.compile(r"\b\d+(?:[.,]\d+)?\b")


def answer_packet(
    client: Any,
    question: str,
    packet: dict,
    *,
    input_usd_per_million: float | None = None,
    output_usd_per_million: float | None = None,
) -> dict:
    if packet.get("aggregate", {}).get("measure") == "active_record_count":
        value = packet["aggregate"]["value"]
        return {
            "status": "supported",
            "answer": f"There are {value} active records.",
            "citation_ids": [],
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": 0.0,
        }
    if packet["status"] != "supported" or not packet["coverage"]:
        return {
            "status": "unknown",
            "answer": "I don't have approved evidence for that answer.",
            "citation_ids": [],
            "input_tokens": 0,
            "output_tokens": 0,
        }
    system = (
        load_skill("retrieving-evidence")
        + "\nAnswer only from the packet. Cite each factual statement with [chunk UUID]. "
        "For semantic facts cite their revision UUID. Say unknown for unsupported parts. "
        "Do not compute totals; only quote SQL aggregates supplied in the packet."
    )
    passages = list(packet["passages"])
    while True:
        content = json.dumps(
            {
                "question": question,
                "blueprint_id": str(packet["blueprint_id"]),
                "exact": packet["exact"],
                "semantic": packet["semantic"],
                "exact_records": packet.get("exact_records", []),
                "passages": passages,
            },
            default=str,
            ensure_ascii=False,
        )
        messages = [{"role": "user", "content": content}]
        counted = client.messages.count_tokens(
            model=MODEL,
            system=system,
            messages=messages,
        ).input_tokens
        if counted <= INPUT_TARGET:
            break
        if not passages:
            raise ValueError("Exact facts and question exceed the input token target")
        passages.pop()
    if (
        not passages
        and not packet["exact"]
        and not packet["semantic"]
        and not packet.get("exact_records")
    ):
        return {
            "status": "unknown",
            "answer": "The supporting passages exceed the context budget.",
            "citation_ids": [],
            "input_tokens": counted,
            "output_tokens": 0,
        }
    if counted > INPUT_HARD_CAP:
        raise ValueError("Input token hard cap exceeded")
    response = client.messages.create(
        model=MODEL, max_tokens=OUTPUT_RESERVE, system=system, messages=messages
    )
    if response.stop_reason != "end_turn":
        raise ValueError("Answer did not finish")
    answer = "\n".join(block.text for block in response.content if block.type == "text")
    allowed = {
        str(row["id"])
        for row in passages + packet["exact"] + packet["semantic"] + packet.get("exact_records", [])
    }
    cited = set(re.findall(r"\[([0-9a-f-]{36})\]", answer))
    cost = None
    if input_usd_per_million is not None and output_usd_per_million is not None:
        cost = round(
            (
                counted * input_usd_per_million
                + response.usage.output_tokens * output_usd_per_million
            )
            / 1_000_000,
            8,
        )
    if not cited or not cited <= allowed:
        return {
            "status": "unknown",
            "answer": "I cannot verify citations for that answer.",
            "failure_reason": "citation",
            "citation_ids": [],
            "input_tokens": counted,
            "output_tokens": response.usage.output_tokens,
            "estimated_cost_usd": cost,
        }
    cited_text = " ".join(
        str(row.get("text", ""))
        + json.dumps(row.get("content", {}), default=str)
        + str(row.get("rationale", ""))
        for row in passages + packet["exact"] + packet["semantic"]
        if str(row["id"]) in cited
    )
    answer_without_citations = re.sub(r"\[[0-9a-f-]{36}\]", "", answer)
    if any(value not in cited_text for value in NUMERIC.findall(answer_without_citations)):
        return {
            "status": "unknown",
            "answer": "I cannot verify a number in that answer.",
            "failure_reason": "citation",
            "citation_ids": [],
            "input_tokens": counted,
            "output_tokens": response.usage.output_tokens,
            "estimated_cost_usd": cost,
        }
    return {
        "status": "supported",
        "answer": answer,
        "citation_ids": sorted(cited),
        "input_tokens": counted,
        "output_tokens": response.usage.output_tokens,
        "estimated_cost_usd": cost,
    }
