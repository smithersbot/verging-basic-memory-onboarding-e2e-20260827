"""Contract checks for the Verging Memory CI adapter.

Runs the adapter in-process against the real Basic Memory implementation
(real projects, real markdown files, real search index) and asserts the
behaviour the Verging adapter contract requires.

    VERGING_PRODUCT_KEY=... python integrations/verging/check_adapter.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

KEY = os.environ.setdefault("VERGING_PRODUCT_KEY", "local-check-key")
DATA_DIR = Path(tempfile.mkdtemp(prefix="verging-adapter-check-"))
os.environ["VERGING_DATA_DIR"] = str(DATA_DIR)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import adapter  # noqa: E402

AUTH = {"Authorization": f"Bearer {KEY}"}

failures: list[str] = []
checks = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


async def recall(client: httpx.AsyncClient, ns: str, query: str, limit: int = 10):
    response = await client.post(
        f"/v1/namespaces/{ns}/memory/recall", json={"query": query, "limit": limit}, headers=AUTH
    )
    return response


async def main() -> int:
    async with adapter.app.router.lifespan_context(adapter.app):
        transport = httpx.ASGITransport(app=adapter.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://adapter", timeout=120.0
        ) as client:
            print("health + authentication")
            response = await client.get("/v1/health")
            check(
                "health is public and ok",
                response.status_code == 200 and response.json() == {"ok": True},
                response.text,
            )

            response = await client.post("/v1/namespaces", json={"name": "x", "forceCreate": True})
            check("no credential -> 401", response.status_code == 401, response.text)

            response = await client.post(
                "/v1/namespaces",
                json={"name": "x", "forceCreate": True},
                headers={"Authorization": "Bearer wrong-key"},
            )
            check("wrong credential -> 401", response.status_code == 401, response.text)

            response = await client.post(
                "/v1/namespaces",
                json={"name": "x", "forceCreate": True},
                headers={"Authorization": KEY},
            )
            check("non-bearer credential -> 401", response.status_code == 401, response.text)

            print("namespace creation")
            response = await client.post(
                "/v1/namespaces",
                json={"name": "verging check one", "forceCreate": True},
                headers=AUTH,
            )
            check(
                "create namespace -> 200 with id",
                response.status_code == 200 and response.json().get("id"),
                response.text,
            )
            ns_one = response.json()["id"]

            response = await client.post(
                "/v1/namespaces",
                json={"name": "verging check one", "forceCreate": True},
                headers=AUTH,
            )
            check(
                "forceCreate makes a second, distinct namespace",
                response.status_code == 200 and response.json()["id"] != ns_one,
                response.text,
            )
            ns_two = response.json()["id"]

            response = await client.post("/v1/namespaces", json={"forceCreate": True}, headers=AUTH)
            check("malformed create -> 4xx", 400 <= response.status_code < 500, response.text)

            print("namespace isolation on disk")
            dirs = sorted(p.name for p in (DATA_DIR / "namespaces").iterdir() if p.is_dir())
            check("each namespace has its own directory", len(dirs) >= 2, str(dirs))

            print("store and recall")
            response = await client.post(
                f"/v1/namespaces/{ns_one}/memory/store",
                json={
                    "content": "Priya prefers her stand-up notes in bullet points, never prose.",
                    "title": "Priya standup preference",
                    "metadata": {"source": "verging-check"},
                },
                headers=AUTH,
            )
            check(
                "store -> ok with id",
                response.status_code == 200
                and response.json().get("ok")
                and response.json().get("id"),
                response.text,
            )
            note_id = response.json()["id"]

            response = await client.post(
                f"/v1/namespaces/{ns_one}/memory/store",
                json={
                    "content": "The deploy window is Thursday 21:00 UTC.",
                    "title": "Deploy window",
                },
                headers=AUTH,
            )
            check(
                "second store -> ok",
                response.status_code == 200 and response.json().get("ok"),
                response.text,
            )
            second_id = response.json()["id"]
            check("distinct notes get distinct ids", second_id != note_id)

            response = await recall(client, ns_one, "bullet points stand-up")
            body = response.json()
            hit_ids = [r["id"] for r in body.get("results", [])]
            check(
                "recall finds the stored note",
                response.status_code == 200 and note_id in hit_ids,
                response.text[:400],
            )
            match = next((r for r in body.get("results", []) if r["id"] == note_id), None)
            check(
                "recall returns real content",
                bool(match and "bullet points" in match["content"]),
                str(match)[:300],
            )
            check(
                "recall carries metadata through",
                bool(match and match.get("metadata", {}).get("source") == "verging-check"),
                str(match)[:300],
            )

            response = await recall(client, ns_one, "deploy window Thursday", limit=1)
            check(
                "recall honours limit",
                response.status_code == 200 and len(response.json()["results"]) <= 1,
                response.text[:300],
            )

            response = await recall(client, ns_one, "nothing here matches xyzzyqux")
            check(
                "recall with no matches -> ok with empty results",
                response.status_code == 200 and response.json()["results"] == [],
                response.text[:300],
            )

            print("update replaces, never duplicates")
            response = await client.post(
                f"/v1/namespaces/{ns_one}/memory/update",
                json={
                    "id": note_id,
                    "content": "Priya now prefers her stand-up notes as a short prose paragraph.",
                    "metadata": {"source": "verging-check", "revision": "2"},
                },
                headers=AUTH,
            )
            check(
                "update -> ok with same id",
                response.status_code == 200 and response.json().get("id") == note_id,
                response.text,
            )

            response = await recall(client, ns_one, "prose paragraph")
            results = response.json()["results"]
            check(
                "updated content is recalled",
                any(r["id"] == note_id and "prose paragraph" in r["content"] for r in results),
                str(results)[:400],
            )
            check(
                "update did not create a duplicate note",
                len([r for r in results if r["id"] == note_id]) == 1,
                str(results)[:300],
            )

            response = await recall(client, ns_one, "bullet points")
            stale = [r for r in response.json()["results"] if "bullet points" in r["content"]]
            check("old content no longer recalled", stale == [], str(stale)[:300])

            files = list((DATA_DIR / "namespaces").rglob("*.md"))
            check(
                "update left one markdown file per note",
                len(files) == 2,
                str([f.name for f in files]),
            )

            response = await client.post(
                f"/v1/namespaces/{ns_one}/memory/update",
                json={"id": "00000000-0000-4000-8000-000000000000", "content": "ghost"},
                headers=AUTH,
            )
            check("update of unknown note -> 404", response.status_code == 404, response.text)

            response = await client.post(
                f"/v1/namespaces/{ns_one}/memory/store", json={"title": "no content"}, headers=AUTH
            )
            check("malformed store -> 4xx", 400 <= response.status_code < 500, response.text)

            print("cross-namespace isolation")
            response = await client.post(
                f"/v1/namespaces/{ns_two}/memory/store",
                json={
                    "content": "Namespace two keeps a secret about bullet points.",
                    "title": "Two",
                },
                headers=AUTH,
            )
            check("store into second namespace -> ok", response.status_code == 200, response.text)
            other_id = response.json()["id"]

            response = await recall(client, ns_one, "secret bullet points")
            check(
                "namespace one cannot see namespace two's note",
                all(r["id"] != other_id for r in response.json()["results"]),
                response.text[:300],
            )

            response = await recall(client, ns_two, "prose paragraph")
            check(
                "namespace two cannot see namespace one's note",
                all(r["id"] != note_id for r in response.json()["results"]),
                response.text[:300],
            )

            response = await client.post(
                f"/v1/namespaces/{ns_two}/memory/update",
                json={"id": note_id, "content": "cross"},
                headers=AUTH,
            )
            check(
                "cannot update another namespace's note", response.status_code == 404, response.text
            )

            print("untrusted identifiers")
            for bad in ["../../etc", "..%2f..%2fetc", "not-a-uuid", "' OR 1=1 --"]:
                response = await client.post(
                    f"/v1/namespaces/{bad}/memory/store", json={"content": "x"}, headers=AUTH
                )
                check(
                    f"traversal-ish namespace {bad!r} rejected",
                    response.status_code in (404, 400, 422),
                    response.text[:200],
                )
            response = await client.post(
                "/v1/namespaces", json={"name": "../../escape", "forceCreate": True}, headers=AUTH
            )
            check(
                "namespace name cannot escape the data root",
                response.status_code == 200,
                response.text,
            )
            escaped = response.json()["id"]
            roots = {p.resolve() for p in (DATA_DIR / "namespaces").iterdir()}
            check(
                "all namespace directories stay under the data root",
                all(str(p).startswith(str((DATA_DIR / "namespaces").resolve())) for p in roots),
                str(roots),
            )
            await client.delete(f"/v1/namespaces/{escaped}", headers=AUTH)

            print("deletion")
            response = await client.delete(f"/v1/namespaces/{ns_two}", headers=AUTH)
            check("delete namespace -> 2xx", 200 <= response.status_code < 300, response.text)
            response = await client.delete(f"/v1/namespaces/{ns_two}", headers=AUTH)
            check("repeat delete is idempotent (404)", response.status_code == 404, response.text)
            response = await client.post(
                f"/v1/namespaces/{ns_two}/memory/store", json={"content": "gone"}, headers=AUTH
            )
            check("store into deleted namespace -> 404", response.status_code == 404, response.text)
            response = await client.delete(f"/v1/namespaces/{ns_one}", headers=AUTH)
            check("delete first namespace -> 2xx", 200 <= response.status_code < 300, response.text)
            leftover = list((DATA_DIR / "namespaces").rglob("*.md"))
            check("deleted namespaces leave no notes behind", leftover == [], str(leftover))

            response = await client.delete(
                "/v1/namespaces/00000000-0000-4000-8000-000000000000", headers=AUTH
            )
            check("delete of unknown namespace -> 404", response.status_code == 404, response.text)
            response = await client.delete(f"/v1/namespaces/{ns_one}")
            check("delete without credential -> 401", response.status_code == 401, response.text)

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    sys.exit(code)
