"""Load the official NIP-44 vectors once and hand them to every test file.

A test asks for a vector by naming a parameter after one of the entries in
`VECTOR_PARAMETERS`; pytest then runs it once per vector. The `nip44_vectors`
fixture gives the whole document to tests that need more than one section.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

VECTORS = json.loads((Path(__file__).parent / "nip44.vectors.json").read_text())["v2"]

# parameter name -> (vectors, test id for each vector)
VECTOR_PARAMETERS: dict[str, tuple[list, Callable[[object], str | None]]] = {
    "conversation_key_vector": (
        VECTORS["valid"]["get_conversation_key"],
        lambda _: None,
    ),
    "message_keys_vector": (
        VECTORS["valid"]["get_message_keys"]["keys"],
        lambda _: None,
    ),
    "padded_length_vector": (VECTORS["valid"]["calc_padded_len"], lambda _: None),
    "encrypt_decrypt_vector": (VECTORS["valid"]["encrypt_decrypt"], lambda _: None),
    "long_message_vector": (
        VECTORS["valid"]["encrypt_decrypt_long_msg"],
        lambda _: None,
    ),
    "invalid_payload_vector": (VECTORS["invalid"]["decrypt"], lambda v: v["note"]),
    "invalid_key_vector": (
        VECTORS["invalid"]["get_conversation_key"],
        lambda v: v["note"],
    ),
    "invalid_length": (VECTORS["invalid"]["encrypt_msg_lengths"], lambda _: None),
}


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    for name, (vectors, label) in VECTOR_PARAMETERS.items():
        if name in metafunc.fixturenames:
            ids = [
                label(vector) or f"vector{index}"
                for index, vector in enumerate(vectors)
            ]
            metafunc.parametrize(name, vectors, ids=ids)


@pytest.fixture(scope="session")
def nip44_vectors() -> dict:
    return VECTORS
