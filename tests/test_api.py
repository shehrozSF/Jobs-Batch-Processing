import pytest
from fastapi.testclient import TestClient

from app.main import app, get_enqueue
from app.tasks import process_job
from tests.conftest import FakeClient, valid_posting


@pytest.fixture
def enqueued():
    """Collect enqueued job ids instead of sending them to Redis."""
    ids: list[int] = []
    app.dependency_overrides[get_enqueue] = lambda: ids.extend
    yield ids
    app.dependency_overrides.clear()


@pytest.fixture
def api(enqueued):
    with TestClient(app) as client:
        yield client


def make_batch(api, n=3):
    postings = [{"source_key": f"k{i}", "body": f"posting {i}"} for i in range(n)]
    response = api.post("/api/batches", json={"name": "t", "postings": postings})
    assert response.status_code == 201
    return response.json()["id"]


def test_creating_a_batch_queues_every_job(api, enqueued):
    batch_id = make_batch(api, 3)
    assert len(enqueued) == 3
    progress = api.get(f"/api/batches/{batch_id}").json()
    assert progress["counts"]["queued"] == 3
    assert progress["complete"] is False


def test_progress_reflects_finished_jobs_and_cost(api, enqueued):
    batch_id = make_batch(api, 2)
    for job_id in enqueued:
        process_job(job_id, client=FakeClient([valid_posting()]))

    progress = api.get(f"/api/batches/{batch_id}").json()
    assert progress["counts"]["succeeded"] == 2
    assert progress["percent"] == 100.0
    assert progress["complete"] is True
    assert progress["model_calls"] == 2
    assert progress["cost_usd"] > 0


def test_retry_requeues_dead_letters(api, enqueued):
    batch_id = make_batch(api, 1)
    process_job(enqueued[0], client=FakeClient([valid_posting(confidence=4)] * 2))
    assert api.get(f"/api/batches/{batch_id}/jobs?status=dead").json()

    enqueued.clear()
    assert api.post(f"/api/batches/{batch_id}/retry").json() == {"requeued": 1}
    assert len(enqueued) == 1


@pytest.mark.parametrize("postings", [
    [],
    [{"source_key": "a", "body": "x"}, {"source_key": "a", "body": "y"}],
])
def test_bad_batches_are_rejected(api, postings):
    response = api.post("/api/batches", json={"name": "t", "postings": postings})
    assert response.status_code == 422


def test_an_unknown_batch_is_404(api):
    assert api.get("/api/batches/999").status_code == 404


def test_the_pages_render(api):
    batch_id = make_batch(api, 1)
    assert api.get("/").status_code == 200
    page = api.get(f"/batches/{batch_id}")
    assert page.status_code == 200 and "hx-get" in page.text  # still polling


def test_upload_creates_a_batch_from_files(api, enqueued):
    files = [("files", ("a.txt", b"posting a")), ("files", ("b.txt", b"posting b"))]
    response = api.post("/batches", files=files, data={"name": "up"}, follow_redirects=False)
    assert response.status_code == 303
    assert len(enqueued) == 2
