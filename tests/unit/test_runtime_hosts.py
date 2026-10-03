"""A signed nonce proves key possession, while operator decisions fence rotations."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from daedalus.extensions.runtime_hosts import RuntimeHosts, _signed_message
from daedalus.stores.control import ControlConflict, Principal
from daedalus.stores.database import Database

OPERATOR = Principal.operator({"via": "cookie", "user_id": 1})


@pytest.fixture
async def hosts(db: Database) -> RuntimeHosts:
    return RuntimeHosts(db)


def public_key(private: Ed25519PrivateKey) -> str:
    return base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def proof(private: Ed25519PrivateKey, host_id: str, challenge: dict, key: str) -> str:
    return base64.b64encode(private.sign(_signed_message(host_id, challenge["nonce"], base64.b64decode(key)))).decode()


@pytest.mark.asyncio
async def test_enrollment_requires_signed_fresh_challenge_and_replays_exactly(hosts: RuntimeHosts) -> None:
    private = Ed25519PrivateKey.generate()
    key = public_key(private)
    first = await hosts.enroll(OPERATOR, label="Worker", public_key=key, expected_collection_revision=1,
                               client_operation_id="enroll-one")
    assert first["identity_state"] == "pending"
    assert await hosts.enroll(OPERATOR, label="Worker", public_key=key, expected_collection_revision=1,
                              client_operation_id="enroll-one") == first
    challenge = await hosts.challenge(OPERATOR, first["host_id"], expected_collection_revision=2,
                                      expected_host_revision=1, client_operation_id="challenge-one")
    assert await hosts.challenge(OPERATOR, first["host_id"], expected_collection_revision=2,
                                 expected_host_revision=1, client_operation_id="challenge-one") == challenge
    with pytest.raises(ValueError, match="did not verify"):
        await hosts.observe(first["host_id"], challenge_id=challenge["challenge_id"], nonce=challenge["nonce"],
                            public_key=key, signature=proof(Ed25519PrivateKey.generate(), first["host_id"], challenge, key))
    signature = proof(private, first["host_id"], challenge, key)
    verified = await hosts.observe(first["host_id"], challenge_id=challenge["challenge_id"],
                                   nonce=challenge["nonce"], public_key=key, signature=signature)
    assert verified["identity_state"] == "verified"
    assert verified["effects_allowed"] is False
    assert await hosts.observe(first["host_id"], challenge_id=challenge["challenge_id"],
                               nonce=challenge["nonce"], public_key=key, signature=signature) == verified
    assert (await hosts.list())["items"][0]["capabilities_state"] == "unknown"
    assert (await hosts.list())["items"][0]["reachability"] == "unknown"


@pytest.mark.asyncio
async def test_rotation_requires_exact_decision_and_new_generation_proof(hosts: RuntimeHosts) -> None:
    old, new = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    old_key, new_key = public_key(old), public_key(new)
    enrolled = await hosts.enroll(OPERATOR, label="Worker", public_key=old_key,
                                  expected_collection_revision=1, client_operation_id="enroll-two")
    host_id = enrolled["host_id"]
    initial = await hosts.challenge(OPERATOR, host_id, expected_collection_revision=2,
                                    expected_host_revision=1, client_operation_id="challenge-two")
    await hosts.observe(host_id, challenge_id=initial["challenge_id"], nonce=initial["nonce"],
                        public_key=old_key, signature=proof(old, host_id, initial, old_key))
    changed = await hosts.challenge(OPERATOR, host_id, expected_collection_revision=3,
                                    expected_host_revision=2, client_operation_id="challenge-three",
                                    candidate_public_key=new_key)
    assert base64.b64decode(changed["message_base64"]) == _signed_message(host_id, changed["nonce"], base64.b64decode(new_key))
    observed = await hosts.observe(host_id, challenge_id=changed["challenge_id"], nonce=changed["nonce"],
                                   public_key=new_key, signature=proof(new, host_id, changed, new_key))
    assert observed["identity_state"] == "changed"
    old_generation = await hosts.challenge(OPERATOR, host_id, expected_collection_revision=4,
                                           expected_host_revision=3, client_operation_id="challenge-before-rotation")
    with pytest.raises(ControlConflict, match="stale"):
        await hosts.decide(OPERATOR, host_id, decision="accept_rotation", reason="confirmed separately",
                           observed_generation=1, expected_host_revision=2,
                           expected_collection_revision=5, client_operation_id="decision-stale")
    approved = await hosts.decide(OPERATOR, host_id, decision="accept_rotation", reason="confirmed separately",
                                  observed_generation=1, expected_host_revision=3,
                                  expected_collection_revision=5, client_operation_id="decision-one")
    assert approved["identity_state"] == "rotating"
    assert approved["host_generation"] == 2
    assert approved["effects_allowed"] is False
    with pytest.raises(ControlConflict, match="earlier or rejected identity"):
        await hosts.observe(host_id, challenge_id=old_generation["challenge_id"], nonce=old_generation["nonce"],
                            public_key=new_key, signature=proof(new, host_id, old_generation, new_key))
    with pytest.raises(ControlConflict, match="earlier or rejected identity"):
        await hosts.observe(host_id, challenge_id=initial["challenge_id"], nonce=initial["nonce"],
                            public_key=old_key, signature=proof(old, host_id, initial, old_key))
    refreshed = await hosts.challenge(OPERATOR, host_id, expected_collection_revision=6,
                                      expected_host_revision=4, client_operation_id="challenge-four")
    with pytest.raises(ControlConflict, match="pinned"):
        await hosts.observe(host_id, challenge_id=refreshed["challenge_id"], nonce=refreshed["nonce"],
                            public_key=old_key, signature=proof(old, host_id, refreshed, old_key))
    final = await hosts.observe(host_id, challenge_id=refreshed["challenge_id"], nonce=refreshed["nonce"],
                                public_key=new_key, signature=proof(new, host_id, refreshed, new_key))
    assert final["identity_state"] == "verified"
    assert final["host_generation"] == 2
