import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP = "com.example.triagedemo"


def crash(cid: str) -> str:
    return (ROOT / "golden" / "crashes" / f"{cid}.txt").read_text()


def mapping(cid: str) -> str:
    return (ROOT / "golden" / "mappings" / f"{cid}.txt").read_text()


@pytest.fixture(scope="session")
def labels() -> dict:
    return {json.loads(l)["id"]: json.loads(l) for l in (ROOT / "golden" / "labels.jsonl").read_text().splitlines() if l.strip()}
