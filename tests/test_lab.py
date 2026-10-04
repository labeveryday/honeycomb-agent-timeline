import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lab.agents import offline_model
from lab.server import roll_back, start_lab
from lab.telemetry import configure
from lab.tools import LabClient
from main import run_scenario


def eval_labels(spans):
    """Eval results recorded on the coordinator's invoke_agent spans, in order."""
    coordinator = sorted((s for s in spans if s.name == "invoke_agent coordinator"), key=lambda s: s.start_time)
    return [e.attributes["gen_ai.evaluation.score.label"] for s in coordinator for e in s.events if e.name == "gen_ai.evaluation.result"]


@pytest.fixture(scope="module")
def telemetry():
    exporter = InMemorySpanExporter()
    provider = configure(exporter=exporter)
    yield exporter
    provider.shutdown()


async def test_broken_retries_ignore_retry_after():
    with start_lab() as (base, state):
        result = await LabClient(base, "broken").fetch("/deployment")
        assert not result["available"] and result["attempts"] == 5
        assert state["requests"]["/deployment"] == 5


async def test_fixed_retry_waits_and_succeeds():
    with start_lab() as (base, _):
        result = await LabClient(base, "fixed").fetch("/deployment")
        assert result["available"] and result["attempts"] == 2
        assert result["data"]["diff"]["INVENTORY_BASE_URL"]["after"] == "/inventory/v1"


async def test_portal_fault_and_manual_rollback():
    with start_lab() as (base, state):
        client = LabClient(base, "fixed")
        assert (await client.check("/portal"))["http_status"] == 502
        assert (await client.check("/inventory/v1"))["http_status"] == 410
        roll_back(state)
        assert (await client.check("/portal"))["http_status"] == 200
        assert (await client.fetch("/deployment"))["data"]["revision"] == "r43"  # the rollback is on record
        assert "14:21" in (await client.fetch("/logs"))["data"]["entries"][-1]
        with pytest.raises(ValueError):
            await client.fetch("https://example.com")


async def test_network_tools_follow_the_request_path():
    with start_lab() as (base, state):
        client = LabClient(base, "fixed")
        assert (await client.dns("portal.lab"))["address"] == "127.0.0.1"
        assert not (await client.dns("db.lab"))["found"]
        assert (await client.tcp("inventory.lab", 80))["open"] and (await client.tcp("inventory.lab", 443))["open"]
        assert not (await client.tcp("inventory.lab", 22))["open"]
        portal = await client.http("http://portal.lab/portal")
        assert portal["http_status"] == 502 and portal["data"]["upstream"] == "http://inventory.lab/inventory/v1"
        assert (await client.http("http://inventory.lab/inventory/v1"))["http_status"] == 410
        assert (await client.http("http://inventory.lab/inventory/v3"))["http_status"] == 404
        assert (await client.http("http://inventory.lab/inventory/"))["http_status"] == 404  # a real 404, not "unreachable"
        assert (await client.http("http://db.lab/"))["error"] == "could not resolve host"
        assert "error" in await client.http("http://portal.lab/deployment")  # deployment history is the release agent's
        assert "/deployment" not in state["requests"]


async def test_broken_run_marks_the_tool_call_failed(telemetry):
    telemetry.clear()
    result = await run_scenario("broken", offline_model())
    assert len(result["turns"]) == 1 and "DEPLOY-01" not in result["turns"][0]["answer"]
    spans = telemetry.get_finished_spans()
    attempts = [s for s in spans if s.name == "GET /deployment"]
    assert [s.attributes["http.response.status_code"] for s in attempts] == [429] * 5
    assert attempts[-1].end_time - attempts[0].start_time < 500_000_000  # all five inside the busy half second
    tool = next(s for s in spans if s.attributes.get("gen_ai.tool.name") == "read_deployment")
    assert not tool.status.is_ok and all(s.parent.span_id == tool.context.span_id for s in attempts)
    assert eval_labels(spans) == ["fail"]  # the cause is a guess without the deployment record


async def test_fixed_run_two_turns_one_conversation(telemetry):
    telemetry.clear()
    result = await run_scenario("fixed", offline_model(), approve=lambda: True)
    first, second = result["turns"]
    assert "DEPLOY-01" in first["answer"] and '\\"inventory_lookup\\": \\"ok\\"' in second["answer"]
    assert "DEPLOY-02" in second["answer"]  # turn 2 sees the rollback deploy
    assert eval_labels(telemetry.get_finished_spans()) == ["pass", "pass"]
    spans = telemetry.get_finished_spans()
    assert len({s.context.trace_id for s in spans}) == 2
    assert all(s.attributes.get("gen_ai.conversation.id") == result["conversation_id"] for s in spans)
    assert {"coordinator", "network", "release"} <= {s.attributes.get("gen_ai.agent.name") for s in spans}

    attempts = sorted((s for s in spans if s.name == "GET /deployment"), key=lambda s: s.start_time)
    attempts = [s for s in attempts if s.context.trace_id == attempts[0].context.trace_id]  # turn 1
    assert [s.attributes["http.response.status_code"] for s in attempts] == [429, 200]
    assert attempts[0].attributes["http.response.header.retry-after"] == ("1",)
    assert attempts[1].start_time - attempts[0].end_time >= 1_000_000_000  # waited for Retry-After
    by_id = {s.context.span_id: s for s in spans}
    assert all(by_id[a.parent.span_id].attributes["gen_ai.tool.name"] == "read_deployment" for a in attempts)
    # Honeycomb: the calling agent emits invoke_agent; the called agent's own spans carry its name.
    invokes = [s for s in spans if s.attributes.get("gen_ai.operation.name") == "invoke_agent"]
    assert invokes and all(s.attributes["gen_ai.agent.name"] == "coordinator" for s in invokes)
    assert all(s.attributes["gen_ai.agent.name"] == "release" for s in spans if s.name == "execute_tool read_deployment")


async def test_fixed_run_without_approval_stops_after_one_turn(telemetry):
    result = await run_scenario("fixed", offline_model())
    assert len(result["turns"]) == 1 and "DEPLOY-01" in result["turns"][0]["answer"]
