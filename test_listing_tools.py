import asyncio
import tempfile
from pathlib import Path

from listing_tools import TitleChanges, fingerprint, view
from listing_tools import PictureChanges
from fastmcp.exceptions import ToolError
import base64
import copy


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


class PictureClient(Client):
    async def get(self, path):
        if path.startswith('/categories/'):
            return {'settings': {'max_pictures_per_item': 12}}
        return copy.deepcopy(self.item)

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.status >= 400:
            return Response(self.status)
        if method == 'POST':
            class Uploaded(Response):
                def json(self):
                    return {'id': 'NEW-PIC'}
            return Uploaded(201)
        self.item['pictures'] = kwargs['json']['pictures']
        return Response(200)


def test_upload_then_publish_preserves_gallery_and_video():
    with tempfile.TemporaryDirectory() as directory:
        original = item()
        original['video_id'] = 'existing-video'
        client = PictureClient(original)
        changes = PictureChanges(Path(directory) / 'changes.sqlite3')
        encoded = base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()
        args = (client, '123', original['id'], encoded, 'photo-upload-0001')
        uploaded = run(changes.upload(*args))
        assert uploaded['published'] is False
        assert run(changes.upload(*args)) == uploaded
        assert len(client.calls) == 1
        ids = ['NEW-PIC', 'PIC1']
        args = (client, '123', original['id'], ids, fingerprint(original), 'photo-gallery-0001')
        result = run(changes.gallery(*args))
        assert result['state'] == 'verified'
        assert result['observed'] == ids
        assert client.item['video_id'] == 'existing-video'
        assert run(changes.gallery(*args)) == result
        assert len(client.calls) == 2


def test_gallery_rejects_deletion_foreign_ids_and_stale_snapshot():
    for ids, snap in [(['NEW-PIC'], fingerprint(item())),
                      (['PIC1', 'FOREIGN'], fingerprint(item())),
                      (['PIC1'], '0' * 64)]:
        with tempfile.TemporaryDirectory() as directory:
            client = PictureClient(item())
            changes = PictureChanges(Path(directory) / 'changes.sqlite3')
            try:
                run(changes.gallery(client, '123', item()['id'], ids, snap, 'photo-invalid-001'))
                raise AssertionError('Must fail closed')
            except ToolError:
                pass
            assert not client.calls


def test_uncertain_upload_blocks_duplicate_operation():
    with tempfile.TemporaryDirectory() as directory:
        client = PictureClient(item(), 500)
        changes = PictureChanges(Path(directory) / 'changes.sqlite3')
        encoded = base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()
        args = (client, '123', item()['id'], encoded, 'photo-uncertain-01')
        assert run(changes.upload(*args))['state'] == 'unknown'
        assert run(changes.upload(*args))['state'] == 'unknown'
        try:
            run(changes.upload(*args[:-1], 'photo-uncertain-02'))
            raise AssertionError('Must block uncertain duplicate')
        except ToolError:
            pass
        assert len(client.calls) == 1
