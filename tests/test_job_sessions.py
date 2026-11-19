"""Aislamiento del portal de trabajos mediante la cookie anónima."""

import hashlib
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vhs import jobs, main  # noqa: E402


def test_cookie_segura_y_trabajos_aislados():
    original_store = main._job_store
    store = jobs.JobStore(Path(tempfile.mkdtemp(prefix="vhs_session_test_")))
    main._job_store = store
    try:
        first = TestClient(main.app, base_url="https://vhs.test")
        initial = first.get("/api/jobs")
        assert initial.status_code == 200
        cookie = first.cookies.get(main.SESSION_COOKIE_NAME)
        assert cookie
        header = initial.headers["set-cookie"].lower()
        assert "httponly" in header and "secure" in header and "samesite=lax" in header

        owner = hashlib.sha256(cookie.encode("utf-8")).hexdigest()
        store.add(jobs.new_job("transcription_batch", {}, owner_id=owner))

        assert len(first.get("/api/jobs").json()["jobs"]) == 1
        second = TestClient(main.app, base_url="https://vhs.test")
        assert second.get("/api/jobs").json()["jobs"] == []
    finally:
        main._job_store = original_store
