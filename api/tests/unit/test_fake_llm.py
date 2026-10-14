import pytest

from ai_router.llm.fake import build_fake_model
from ai_router.mail.gateway import InMemoryMailGateway
from tests.conftest import POLICY
from tests.unit.test_agent import setup

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Nie działa mi komputer", "help-desk@example.com"),
        ("Chciałbym zgłosić urlop na jutro", "kadry@example.com"),
        ("Serwer produkcyjny nie odpowiada", "it@example.com"),
        ("Pytanie o kartę sportową i benefit", "human-resources@example.com"),
        ("Gdzie mogę zaparkować rower?", "other@example.com"),
    ],
)
@pytest.mark.parametrize("mode", ["tool", "output_function"])
async def test_fake_model_routes_by_keywords(message: str, expected: str, mode: str) -> None:
    ra, deps, mailbox, _ = setup(message=message, agent_mode=mode)
    await ra.agent.run(
        f"Route the following message.\n\n<user_message>\n{message}\n</user_message>",
        deps=deps,
        model=build_fake_model(POLICY),
    )
    assert isinstance(mailbox, InMemoryMailGateway)
    assert [m["To"] for m in mailbox.messages] == [expected]
    assert deps.sent is not None
    assert deps.sent.needs_review is (
        expected == "other@example.com"
    )  # fallback has low confidence
