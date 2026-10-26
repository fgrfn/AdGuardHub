"""Exporting the hub's configuration and putting it back.

Everything the hub knows lives in one SQLite file, so this is the difference
between a wiped volume being an inconvenience and being the end of the setup.
That makes the failure modes worth stating: a backup that quietly carries
passwords, a bad file that destroys the data it was meant to protect, and a
restore that strips the credentials off a node that was working.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.services import backup as backup_service

from .fakes import FakeAdapter
from .test_sync import A, B, add_instance


async def _populate(client: httpx.AsyncClient) -> None:
    await add_instance(client, "node-a", A)
    await client.post("/api/rules", json={"text": "||ads.example.com^", "kind": "block"})
    await client.post(
        "/api/filter-lists",
        json={"name": "A list", "url": "https://example.com/list.txt", "kind": "blocklist"},
    )
    await client.patch(
        "/api/config/sections/access",
        json={"managed": True, "data": {"disallowed_clients": ["10.0.0.9"]}},
    )


async def test_the_export_carries_the_whole_configuration(
    auth_client: httpx.AsyncClient,
) -> None:
    await _populate(auth_client)

    response = await auth_client.get("/api/backup")
    assert response.status_code == 200
    # Offered as a file rather than rendered, or a browser just shows the JSON.
    assert "attachment" in response.headers["content-disposition"]

    document = response.json()
    assert document["format"] == "adguardhub-backup"
    assert [rule["text"] for rule in document["snapshot"]["rules"]] == ["||ads.example.com^"]
    assert document["snapshot"]["filter_lists"][0]["url"] == "https://example.com/list.txt"
    assert document["snapshot"]["sections"]["access"]["data"] == {
        "disallowed_clients": ["10.0.0.9"]
    }
    assert [item["name"] for item in document["instances"]] == ["node-a"]


async def test_no_password_leaves_in_a_backup(auth_client: httpx.AsyncClient) -> None:
    """The one property this file must have, whatever else changes.

    A backup is downloaded through a browser and then lives wherever the operator
    puts it. Ciphertext would be no better: it is one leaked secret key away from
    the plaintext, and the key is the thing most likely to be in the same backup
    folder.
    """
    await add_instance(auth_client, "node-a", A)

    raw = (await auth_client.get("/api/backup")).text

    assert "password" not in raw.lower()
    assert "secret" not in raw.lower()
    exported = json.loads(raw)["instances"][0]
    assert set(exported) == set(backup_service.INSTANCE_FIELDS)


async def test_a_backup_restores_onto_an_empty_hub(auth_client: httpx.AsyncClient) -> None:
    await _populate(auth_client)
    document = (await auth_client.get("/api/backup")).json()

    # Wipe the hub the way losing the volume would.
    rules = (await auth_client.get("/api/rules")).json()
    for rule in rules:
        await auth_client.delete(f"/api/rules/{rule['id']}")
    lists = (await auth_client.get("/api/filter-lists")).json()
    for item in lists:
        await auth_client.delete(f"/api/filter-lists/{item['id']}")
    assert (await auth_client.get("/api/rules")).json() == []

    response = await auth_client.post("/api/backup/restore", json=document)
    assert response.status_code == 200, response.text

    restored = (await auth_client.get("/api/rules")).json()
    assert [rule["text"] for rule in restored] == ["||ads.example.com^"]
    sections = {
        item["name"]: item for item in (await auth_client.get("/api/config/sections")).json()
    }
    assert sections["access"]["data"] == {"disallowed_clients": ["10.0.0.9"]}


async def test_a_restored_instance_is_reported_as_needing_its_password(
    auth_client: httpx.AsyncClient,
) -> None:
    """The node comes back, its credentials do not — so the count says so."""
    await add_instance(auth_client, "node-a", A)
    document = (await auth_client.get("/api/backup")).json()

    instances = (await auth_client.get("/api/instances")).json()
    await auth_client.delete(f"/api/instances/{instances[0]['id']}")

    body = (await auth_client.post("/api/backup/restore", json=document)).json()
    assert body["instances_added"] == 1
    assert body["instances_need_password"] == 1

    back = (await auth_client.get("/api/instances")).json()
    assert [item["name"] for item in back] == ["node-a"]
    assert back[0]["has_password"] is False


async def test_a_restore_leaves_a_working_instance_alone(
    auth_client: httpx.AsyncClient,
) -> None:
    """Restoring onto a live hub must not strip credentials off a connected node.

    The backup has no password to put back, so overwriting the existing row would
    turn a working node into an unreachable one — a restore that breaks the thing
    it was run to protect.
    """
    await add_instance(auth_client, "node-a", A)
    document = (await auth_client.get("/api/backup")).json()

    body = (await auth_client.post("/api/backup/restore", json=document)).json()
    assert body["instances_added"] == 0

    instances = (await auth_client.get("/api/instances")).json()
    assert len(instances) == 1
    assert instances[0]["has_password"] is True


async def test_a_second_node_at_a_new_url_is_added(auth_client: httpx.AsyncClient) -> None:
    await add_instance(auth_client, "node-a", A)
    await add_instance(auth_client, "node-b", B)
    document = (await auth_client.get("/api/backup")).json()

    instances = (await auth_client.get("/api/instances")).json()
    await auth_client.delete(f"/api/instances/{instances[1]['id']}")

    body = (await auth_client.post("/api/backup/restore", json=document)).json()
    assert body["instances_added"] == 1
    assert len((await auth_client.get("/api/instances")).json()) == 2


def _backup(snapshot: dict, instances: list | None = None) -> str:
    """A file that is a backup in every respect except the parts given."""
    return json.dumps(
        {
            "format": "adguardhub-backup",
            "format_version": 1,
            "snapshot": {"rules": [], "filter_lists": [], "sections": {}, **snapshot},
            "instances": instances or [],
        }
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("not json at all", "not valid JSON"),
        (json.dumps([1, 2, 3]), "not an AdGuardHub backup"),
        (json.dumps({"format": "something-else"}), "not an AdGuardHub backup"),
        (json.dumps({"format": "adguardhub-backup", "format_version": 99}), "format version"),
        (
            json.dumps({"format": "adguardhub-backup", "format_version": 1}),
            "no configuration in it",
        ),
        (
            json.dumps(
                {
                    "format": "adguardhub-backup",
                    "format_version": 1,
                    "snapshot": {"rules": "nope"},
                }
            ),
            "damaged",
        ),
        # The lists have the right shape; what is in them does not. Each of
        # these used to get past validation and then either crashed the restore
        # halfway through (a 500, with the rule tables already emptied in the
        # transaction) or was written and broke whatever read it back.
        (_backup({"rules": ["@@||bare-string.example^"]}), "rule 1 is damaged"),
        (_backup({"rules": [{"text": 42}]}), "rule 1 has a 'text' that is not text"),
        (_backup({"rules": [{"text": "||x^", "kind": "sideways"}]}), "expected one of"),
        (_backup({"filter_lists": [{"url": 5}]}), "subscription 1 has a 'url'"),
        (_backup({"filter_lists": [{"url": "https://x", "kind": "greylist"}]}), "expected one of"),
        (_backup({"sections": {"dns": []}}), "section 'dns' is damaged"),
        (_backup({"sections": {"dns": {"managed": True, "data": "x"}}}), "section 'dns'"),
        (_backup({}, instances=["node"]), "instance 1 is damaged"),
        (
            _backup({}, instances=[{"name": "n", "base_url": "ftp://node.example"}]),
            "invalid address",
        ),
        (
            _backup({}, instances=[{"name": "n", "base_url": "http://user:pw@node.example"}]),
            "invalid address",
        ),
        (
            _backup({}, instances=[{"name": "n", "base_url": "http://n", "adapter": "pihole"}]),
            "adapter this hub does not have",
        ),
    ],
)
async def test_a_bad_file_is_refused_before_anything_is_deleted(
    auth_client: httpx.AsyncClient, payload: str, expected: str
) -> None:
    """The data has to still be there when the file turns out to be wrong."""
    await _populate(auth_client)
    before = (await auth_client.get("/api/rules")).json()

    response = await auth_client.post(
        "/api/backup/restore",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert expected in response.json()["detail"]

    assert (await auth_client.get("/api/rules")).json() == before


async def test_the_restore_itself_can_be_rolled_back(
    auth_client: httpx.AsyncClient,
) -> None:
    """Restoring the wrong file is a change like any other, so it is undoable.

    Nothing extra is snapshotted on the way in: every change already records one,
    so the state a restore replaces is by construction the newest version in the
    history, and rolling back to it is the undo.
    """
    await _populate(auth_client)
    document = (await auth_client.get("/api/backup")).json()

    await auth_client.post("/api/rules", json={"text": "||later.example.com^", "kind": "block"})
    replaced_state = (await auth_client.get("/api/versions")).json()[0]

    await auth_client.post("/api/backup/restore", json=document)
    texts = {rule["text"] for rule in (await auth_client.get("/api/rules")).json()}
    assert texts == {"||ads.example.com^"}, "the backup's state replaced the newer rule"

    labels = [item["label"] for item in (await auth_client.get("/api/versions")).json()]
    assert "restored from a backup file" in labels

    await auth_client.post(f"/api/versions/{replaced_state['id']}/restore")
    texts = {rule["text"] for rule in (await auth_client.get("/api/rules")).json()}
    assert "||later.example.com^" in texts


async def test_a_restore_reaches_the_instances(auth_client: httpx.AsyncClient) -> None:
    from app.services.sync import drain_background

    await _populate(auth_client)
    document = (await auth_client.get("/api/backup")).json()
    await auth_client.post("/api/rules", json={"text": "||gone.example.com^", "kind": "block"})
    await drain_background()

    await auth_client.post("/api/backup/restore", json=document)
    await drain_background()

    assert FakeAdapter.state_for(A).rules == ["||ads.example.com^"]
