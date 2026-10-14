import pytest
import yaml

from tests.conftest import ApiFactory
from tests.helpers.scripted_model import text, tool_call

pytestmark = pytest.mark.anyio

PAYLOAD = {"email": "jan@example.com", "message": "Nie działa mi komputer"}


async def routed_ticket(h) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    body = (await h.client.post("/api/v1/messages", json=PAYLOAD)).json()
    ticket_id = body["ticket_id"]
    signer = h.container.tickets.signer
    return ticket_id, signer.sign(ticket_id)


async def test_feedback_link_form_and_correction_flow(api: ApiFactory) -> None:
    async with api(tool_call("it@example.com"), text()) as h:
        ticket_id, token = await routed_ticket(h)
        email_text = h.smtp.sent[0].get_body(("plain",)).get_content()
        assert f"/api/v1/feedback/{ticket_id}?token={token}" in email_text

        form = await h.client.get(f"/api/v1/feedback/{ticket_id}", params={"token": token})
        assert form.status_code == 200 and "help-desk@example.com" in form.text

        thanks = await h.client.post(
            f"/api/v1/feedback/{ticket_id}",
            params={"token": token},
            data={
                "correct_department": "help-desk@example.com",
                "comment": "To sprawa jednej osoby",
            },
        )
        assert thanks.status_code == 200 and "Dziękujemy" in thanks.text

        export = await h.client.get("/api/v1/feedback/export")
    cases = yaml.safe_load(export.text)
    assert cases == [
        {
            "id": f"fb-{ticket_id[:8]}",
            "message": "Nie działa mi komputer",
            "expected": "help-desk@example.com",
            "tags": ["feedback", "real"],
            "routed_as": "it@example.com",
        }
    ]


async def test_feedback_json_confirmation(api: ApiFactory) -> None:
    async with api(tool_call("help-desk@example.com"), text()) as h:
        ticket_id, token = await routed_ticket(h)
        response = await h.client.post(
            f"/api/v1/feedback/{ticket_id}",
            params={"token": token},
            json={"correct_department": "help-desk@example.com"},
        )
        unknown = await h.client.post(
            f"/api/v1/feedback/{ticket_id}",
            params={"token": token},
            json={"correct_department": "nobody@example.com"},
        )
    assert response.json() == {"ticket_id": ticket_id, "verdict": "confirmed"}
    assert unknown.status_code == 422


async def test_forged_token_looks_like_unknown_ticket(api: ApiFactory) -> None:
    async with api(tool_call(), text()) as h:
        ticket_id, _ = await routed_ticket(h)
        response = await h.client.get(f"/api/v1/feedback/{ticket_id}", params={"token": "0" * 32})
    assert response.status_code == 404


async def test_export_requires_admin_token_when_configured(api: ApiFactory) -> None:
    async with api(admin_token="s3cret") as h:
        denied = await h.client.get("/api/v1/feedback/export")
        allowed = await h.client.get(
            "/api/v1/feedback/export", headers={"Authorization": "Bearer s3cret"}
        )
    assert denied.status_code == 403
    assert allowed.status_code == 200 and allowed.text.strip() == "[]"
