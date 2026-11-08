"""Render backlogs must yield to the source polling loop."""
from types import SimpleNamespace

from newsroom.service import ServiceLoop


def test_run_polls_between_queued_renders(conn, settings, monkeypatch):
    from newsroom.ingestion import polling
    from newsroom.storage import repositories as repo

    queue = [
        SimpleNamespace(job_id=str(i), job_type="SHORT_RENDER",
                        priority=30, event_id=f"event:{i}")
        for i in range(3)
    ]
    polls, rendered, completed = [], [], []
    monkeypatch.setattr(polling, "poll_once",
                        lambda *args: polls.append("poll") or {"errors": []})
    monkeypatch.setattr(repo, "claim_next_job",
                        lambda conn: queue.pop(0) if queue else None)
    monkeypatch.setattr(repo, "complete_job",
                        lambda conn, job_id: completed.append(job_id))
    loop = ServiceLoop(conn, settings)
    monkeypatch.setattr(loop, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(loop, "_run_job",
                        lambda kind, event_id: rendered.append(event_id))
    monkeypatch.setattr(loop, "_sleep",
                        lambda seconds: setattr(loop, "_stop", len(polls) == 2))

    loop.run(60)

    assert len(polls) == 2
    assert rendered == ["event:0", "event:1"]
    assert completed == ["0", "1"]
    assert [job.event_id for job in queue] == ["event:2"]
