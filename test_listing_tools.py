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


def staged_replacement(directory, original=None, status=200):
    original = original or item()
    client = PictureClient(original, status)
    changes = PictureChanges(Path(directory) / 'changes.sqlite3')
    encoded = base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()
    run(changes.upload(client, '123', original['id'], encoded, 'replace-upload-001'))
    return client, changes


def test_replace_exact_photo_preserves_rest_and_is_idempotent():
    with tempfile.TemporaryDirectory() as directory:
        original = item()
        original['pictures'].append({'id': 'KEEP'})
        original['video_id'] = 'video'
        client, changes = staged_replacement(directory, original)
        args = (client, '123', original['id'], ['NEW-PIC', 'KEEP'],
                fingerprint(original), 'replace-gallery-001')
        result = run(changes.gallery(*args, remove_picture_ids=['PIC1']))
        assert result['state'] == 'verified'
        assert result['before'] == ['PIC1', 'KEEP']
        assert result['removed'] == ['PIC1']
        assert result['other_settings_preserved'] is True
        assert run(changes.gallery(*args, remove_picture_ids=['PIC1'])) == result
        assert len(client.calls) == 2
        assert client.calls[-1][2]['json'] == {'pictures': [{'id': 'NEW-PIC'}, {'id': 'KEEP'}]}


def test_replace_rejects_undeclared_deletions_foreign_photos_and_stale_reads():
    cases = [(['NEW-PIC'], ['OTHER'], None),
             (['NEW-PIC'], ['PIC1', 'PIC1'], None),
             (['FOREIGN'], ['PIC1'], None),
             (['NEW-PIC', 'PIC1'], ['PIC1'], None),
             (['NEW-PIC'], ['PIC1'], '0' * 64),
             (['NEW-PIC'], [], None)]
    for ids, removed, snap in cases:
        with tempfile.TemporaryDirectory() as directory:
            client, changes = staged_replacement(directory)
            try:
                run(changes.gallery(client, '123', item()['id'], ids,
                                    snap or fingerprint(item()), 'replace-invalid-001',
                                    remove_picture_ids=removed))
                raise AssertionError('Must fail closed')
            except ToolError:
                pass
            assert len(client.calls) == 1  # Only the staging upload.


def test_replace_blocks_variant_linked_photo():
    with tempfile.TemporaryDirectory() as directory:
        original = item()
        original['variations'] = [{'id': 1, 'picture_ids': ['PIC1']}]
        client, changes = staged_replacement(directory, original)
        try:
            run(changes.gallery(client, '123', original['id'], ['NEW-PIC'],
                                fingerprint(original), 'replace-linked-001',
                                remove_picture_ids=['PIC1']))
            raise AssertionError('Must fail closed')
        except ToolError:
            pass
        assert len(client.calls) == 1


def test_uncertain_replacement_keeps_lock_and_never_resends():
    with tempfile.TemporaryDirectory() as directory:
        client, changes = staged_replacement(directory)
        client.status = 500
        args = (client, '123', item()['id'], ['NEW-PIC'], fingerprint(item()), 'replace-unknown-001')
        first = run(changes.gallery(*args, remove_picture_ids=['PIC1']))
        assert first['state'] == 'unknown'
        assert run(changes.gallery(*args, remove_picture_ids=['PIC1'])) == first
        try:
            run(changes.gallery(*args[:-1], 'replace-unknown-002', remove_picture_ids=['PIC1']))
            raise AssertionError('Must block duplicate write')
        except ToolError:
            pass
        assert len(client.calls) == 2


def deletion_item():
    original = item()
    original['pictures'] = [{'id': p} for p in ['PIC1', 'PIC2', 'PIC3']]
    original['video_id'] = 'keep-video'
    return original


def test_delete_cover_without_replacement_preserves_order_and_is_idempotent(tmp_path):
    original = deletion_item()
    client = PictureClient(original)
    changes = PictureChanges(tmp_path / 'changes.sqlite3')
    args = (client, '123', original['id'], ['PIC2', 'PIC3'],
            fingerprint(original), 'delete-cover-001')
    result = run(changes.gallery(*args, remove_picture_ids=['PIC1'], delete_only=True))
    assert result['state'] == 'verified'
    assert result['kind'] == 'gallery_delete'
    assert result['before'] == ['PIC1', 'PIC2', 'PIC3']
    assert result['removed'] == ['PIC1']
    assert result['other_settings_preserved'] is True
    assert run(changes.gallery(*args, remove_picture_ids=['PIC1'], delete_only=True)) == result
    assert client.calls == [('PUT', '/items/' + original['id'],
                             {'json': {'pictures': [{'id': 'PIC2'}, {'id': 'PIC3'}]}})]


def test_delete_rejects_invalid_scope_and_changed_or_foreign_listing(tmp_path):
    import pytest
    cases = [([], ['PIC1', 'PIC2', 'PIC3'], '123', None),
             (['PIC2', 'PIC3'], [], '123', None),
             (['PIC2', 'PIC3'], ['PIC1', 'PIC1'], '123', None),
             (['PIC2', 'PIC3'], ['FOREIGN'], '123', None),
             (['PIC3', 'PIC2'], ['PIC1'], '123', None),
             (['NEW', 'PIC2', 'PIC3'], ['PIC1'], '123', None),
             (['PIC3'], ['PIC1'], '123', None),
             (['PIC2', 'PIC3'], ['PIC1'], '999', None),
             (['PIC2', 'PIC3'], ['PIC1'], '123', '0' * 64)]
    for index, (ids, removed, seller, snap) in enumerate(cases):
        original = deletion_item()
        client = PictureClient(original)
        changes = PictureChanges(tmp_path / f'{index}.sqlite3')
        with pytest.raises(ToolError):
            run(changes.gallery(client, seller, original['id'], ids,
                                snap or fingerprint(original), 'delete-invalid-001',
                                remove_picture_ids=removed, delete_only=True))
        assert client.calls == []


def test_delete_protects_variant_photos(tmp_path):
    import pytest
    original = deletion_item()
    original['variations'] = [{'id': 1, 'picture_ids': ['PIC1']}]
    client = PictureClient(original)
    changes = PictureChanges(tmp_path / 'changes.sqlite3')
    with pytest.raises(ToolError):
        run(changes.gallery(client, '123', original['id'], ['PIC2', 'PIC3'],
                            fingerprint(original), 'delete-linked-001',
                            remove_picture_ids=['PIC1'], delete_only=True))
    assert client.calls == []


def test_delete_uncertain_result_blocks_retry_with_new_id(tmp_path):
    import pytest
    original = deletion_item()
    client = PictureClient(original, status=500)
    changes = PictureChanges(tmp_path / 'changes.sqlite3')
    args = (client, '123', original['id'], ['PIC2', 'PIC3'],
            fingerprint(original), 'delete-unknown-001')
    first = run(changes.gallery(*args, remove_picture_ids=['PIC1'], delete_only=True))
    assert first['state'] == 'unknown'
    assert run(changes.gallery(*args, remove_picture_ids=['PIC1'], delete_only=True)) == first
    with pytest.raises(ToolError):
        run(changes.gallery(*args[:-1], 'delete-unknown-002',
                            remove_picture_ids=['PIC1'], delete_only=True))
    assert len(client.calls) == 1


def test_delete_does_not_claim_success_when_gallery_does_not_match(tmp_path):
    class MismatchClient(PictureClient):
        async def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return Response(200)
    original = deletion_item()
    client = MismatchClient(original)
    changes = PictureChanges(tmp_path / 'changes.sqlite3')
    result = run(changes.gallery(client, '123', original['id'], ['PIC2', 'PIC3'],
                                fingerprint(original), 'delete-mismatch-001',
                                remove_picture_ids=['PIC1'], delete_only=True))
    assert result['state'] == 'verification_mismatch'


def test_delete_tool_requires_confirmation_and_registers_destructive_hint(tmp_path):
    import pytest
    from listing_tools import register
    class Registry:
        def __init__(self):
            self.functions = {}
            self.annotations = {}
        def tool(self, annotations):
            def decorate(function):
                self.functions[function.__name__] = function
                self.annotations[function.__name__] = annotations
                return function
            return decorate
    registry = Registry()
    original = deletion_item()
    client = PictureClient(original)
    register(registry, lambda: client, '123', tmp_path)
    delete = registry.functions['nf_fotos_eliminar']
    assert registry.annotations['nf_fotos_eliminar']['destructiveHint'] is True
    args = (original['id'], ['PIC2', 'PIC3'], ['PIC1'], fingerprint(original), 'delete-tool-001')
    with pytest.raises(ToolError):
        run(delete(*args, confirmacion=''))
    assert client.calls == []
    assert run(delete(*args, confirmacion='ELIMINAR_FOTOS'))['state'] == 'verified'
