import asyncio
import tempfile
from pathlib import Path

from listing_tools import TitleChanges, fingerprint, view


class Response:
    def __init__(self, status_code):
        self.status_code = status_code


class Client:
    def __init__(self, item, status=200):
        self.item = dict(item)
        self.status = status
        self.calls = []

    async def get(self, path):
        return dict(self.item)

    async def request(self, method, path, json):
        self.calls.append((method, path, json))
        if 200 <= self.status < 300:
            self.item["title"] = json["title"]
        return Response(self.status)


def item():
    return {
        "id": "MLA3477871304", "seller_id": 123, "title": "Título anterior",
        "status": "active", "price": 16990, "available_quantity": 36,
        "category_id": "MLA4559", "domain_id": "MLA-WRIST_SUPPORTS",
        "user_product_id": "MLAU4127850954", "catalog_listing": False,
        "listing_type_id": "gold_special", "currency_id": "ARS",
        "channels": ["marketplace"], "variations": [], "pictures": [{"id": "PIC1"}],
    }


def run(coro):
    return asyncio.run(coro)


def test_view_has_snapshot():
    data = view(item())
    assert data["title"] == "Título anterior"
    assert data["snapshot_hash"] == fingerprint(item())


def test_verified_title_change_preserves_other_settings():
    with tempfile.TemporaryDirectory() as directory:
        original = item()
        client = Client(original)
        changes = TitleChanges(Path(directory) / "changes.sqlite3")
        result = run(changes.set(
            client, "123", original["id"], "Título nuevo", original["title"],
            fingerprint(original), "title-op-0001",
        ))
        assert result["state"] == "verified"
        assert result["observed"] == "Título nuevo"
        assert result["other_settings_preserved"] is True
        assert client.calls == [("PUT", "/items/MLA3477871304", {"title": "Título nuevo"})]


def test_rejected_write_is_not_retried():
    with tempfile.TemporaryDirectory() as directory:
        original = item()
        client = Client(original, status=400)
        changes = TitleChanges(Path(directory) / "changes.sqlite3")
        args = (client, "123", original["id"], "Título nuevo", original["title"],
                fingerprint(original), "title-op-0002")
        first = run(changes.set(*args))
        second = run(changes.set(*args))
        assert first["state"] == "rejected"
        assert second == first
        assert len(client.calls) == 1
