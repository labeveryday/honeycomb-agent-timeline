"""Investigate the portal incident with retry version 1 (broken) or version 2 (fixed)."""
import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from lab.agents import LIMITS, build_agents, offline_model, run_with_steps
from lab.server import FIXTURES, roll_back, start_lab
from lab.telemetry import configure
from lab.tools import LabClient

FOLLOW_UP = "I rolled INVENTORY_BASE_URL back to /inventory/v2. Check whether the portal works now."


async def run_turn(coordinator, prompt, base, retries, conversation, history=None):
    """One message to the coordinator. Each turn is one coordinator run, so PydanticAI gives it its own trace."""
    async with asyncio.timeout(120):
        return await run_with_steps(coordinator, prompt, deps=LabClient(base, retries), message_history=history,
                                    conversation_id=conversation, usage_limits=LIMITS)


async def run_scenario(scenario, model, conversation=None, approve=lambda: False):
    conversation = conversation or str(uuid4())
    coordinator = build_agents(model)
    history, turns, prompt = None, [], FIXTURES["symptom"]
    with start_lab() as (base, state):
        for turn in (1, 2):
            if turn == 2:
                # Only the fixed run finds what to roll back, and only you can approve it.
                if scenario != "fixed" or not approve():
                    break
                roll_back(state)  # The human applies the recommended rollback (deploy r43).
                print("\n(You roll back INVENTORY_BASE_URL to /inventory/v2 as deploy r43.)")
                prompt = FOLLOW_UP
            print(f"\n=== Turn {turn}" + (f' · Prompt: "{prompt}"' if turn > 1 else "") + "\n")
            result = await run_turn(coordinator, prompt, base, scenario, conversation, history)
            print(f"\n=== Turn {turn} · coordinator's answer\n{result.output}")
            history = result.all_messages()
            turns.append({"turn": turn, "prompt": prompt, "answer": result.output})
        return {"scenario": scenario, "conversation_id": conversation, "turns": turns, "http_requests": state["requests"].copy()}


def ask_to_roll_back():
    try:
        answer = input("\nApply the rollback and send a follow-up message in the same conversation? [y/N] ")
    except EOFError:  # not an interactive terminal
        return False
    return answer.strip().lower() == "y"


def pick_model(parser, offline):
    """The scripted smoke model offline; otherwise MODEL from .env, as PydanticAI's provider:model."""
    if offline:
        return offline_model()
    model = os.getenv("MODEL", "")
    model = model if ":" in model else f"anthropic:{model}"
    if not os.getenv("ANTHROPIC_API_KEY") or not model.startswith("anthropic:claude-"):
        parser.error("Set ANTHROPIC_API_KEY and MODEL (for example claude-sonnet-5-5) in .env, or use --offline.")
    return model


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["broken", "fixed"], default="fixed",
                        help="broken: retry version 1. fixed: version 2, then asks whether to send a follow-up (y = two traces)")
    parser.add_argument("--offline", action="store_true", help="Scripted plumbing test, no AI reasoning or API costs")
    args = parser.parse_args()
    model = pick_model(parser, args.offline)
    # Readable and sortable, in the artifacts folder and in Honeycomb's conversation list.
    run_id = f"{args.scenario}{'-offline' if args.offline else ''}-{datetime.now().astimezone():%Y%m%d-%H%M%S}"
    folder = Path("artifacts") / run_id
    provider = configure(folder / "spans.jsonl")
    print(f"Scenario: {args.scenario} ({'scripted-smoke-test' if args.offline else 'live-model'}) · Conversation ID: {run_id}")
    print(f'Prompt: "{FIXTURES["symptom"]}"')
    try:
        result = asyncio.run(run_scenario(args.scenario, model, run_id, approve=ask_to_roll_back))
        result["execution_mode"] = "scripted-smoke-test" if args.offline else "live-model"
        (folder / "result.json").write_text(json.dumps(result, indent=2))
        print(f"\nHTTP requests to the lab: {result['http_requests']}")
        print(f"Artifacts: {folder}")
    except Exception as exc:  # noqa: BLE001
        # Report any failure in one line. Error text says what went wrong; it never contains the API key.
        print(f"Run stopped: {type(exc).__name__}: {str(exc)[:500]}\nNo remediation was executed.")
        raise SystemExit(1) from None
    finally:
        provider.force_flush(timeout_millis=10000)
        provider.shutdown()


if __name__ == "__main__":
    main()
