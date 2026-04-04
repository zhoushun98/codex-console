import asyncio

from src.web.routes import registration


def test_classify_registration_task_outcome_marks_degraded_and_fallback_success():
    task = type(
        "Task",
        (),
        {
            "status": "completed",
            "result": {
                "metadata": {
                    "completion_path": "anyauto_fallback",
                    "token_pending": True,
                    "fallback_attempted": True,
                }
            },
            "error_message": None,
        },
    )()

    outcome = registration._classify_registration_task_outcome(task)

    assert outcome["degraded_success"] is True
    assert outcome["fallback_success"] is True
    assert outcome["retryable_failure"] is False
    assert outcome["token_pending"] is True


def test_classify_registration_task_outcome_marks_retryable_failure():
    task = type(
        "Task",
        (),
        {
            "status": "failed",
            "result": None,
            "error_message": "跟随重定向链失败",
        },
    )()

    outcome = registration._classify_registration_task_outcome(task)

    assert outcome["degraded_success"] is False
    assert outcome["fallback_success"] is False
    assert outcome["retryable_failure"] is True


def test_sleep_pipeline_interval_uses_max_wait_when_flag_set(monkeypatch):
    batch_id = "batch-test"
    registration.batch_tasks[batch_id] = {"next_wait_force_max": True}
    sleeps = []
    logs = []
    original_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await original_sleep(0)

    monkeypatch.setattr(registration.random, "randint", lambda _min, _max: _min)
    monkeypatch.setattr(registration.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(registration.logger, "info", lambda *args, **kwargs: None)

    try:
        asyncio.run(registration._sleep_pipeline_interval(batch_id, 3, 9, logs.append))
    finally:
        registration.batch_tasks.pop(batch_id, None)

    assert sleeps == [9]
    assert any("最大值 9 秒" in msg for msg in logs)
