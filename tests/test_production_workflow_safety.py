from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAFETY_ROOT = ROOT / "systemctl" / "production-237"
QUEUES = (
    "orch_dispatch,orch_execute,orch_switch_bot_flow,"
    "orch_channel_supplier_v2,orch_heartbeat"
)


def test_production_worker_dropins_pin_safe_concurrency() -> None:
    for index in range(1, 6):
        unit = f"orch-celery-worker_{index:02d}.service"
        path = SAFETY_ROOT / f"{unit}.d" / "zz-db-safety-autoscale.conf"
        content = path.read_text(encoding="utf-8")

        assert content.startswith("[Service]\nExecStart=\n")
        assert f"--hostname=orch-celery-worker@237_{index:02d}" in content
        assert f"--queues={QUEUES}" in content
        assert "--autoscale=8,2" in content
        assert "--prefetch-multiplier=1 -Ofair" in content
        assert "--max-tasks-per-child=1000" in content
        assert "--max-memory-per-child=350000" in content


def test_production_beat_dropin_pins_guarded_dispatch() -> None:
    content = (
        SAFETY_ROOT
        / "orch-celery-beat.service.d"
        / "zz-incident-dispatch-safe.conf"
    ).read_text(encoding="utf-8")

    assert "Environment=CELERY_DISPATCH_INTERVAL_SECONDS=5" in content
    assert "Environment=CELERY_DISPATCH_BATCH_SIZE=10" in content


def test_installer_checks_every_versioned_dropin() -> None:
    content = (ROOT / "scripts" / "prod_workflow_safety_237.sh").read_text(
        encoding="utf-8"
    )

    for index in range(1, 6):
        assert f"orch-celery-worker_{index:02d}.service" in content
    assert "orch-celery-beat.service" in content
    assert 'cmp -s "${source}" "${target}"' in content
    assert "--autoscale=8,2" in content
    assert "CELERY_DISPATCH_INTERVAL_SECONDS=5" in content
    assert "CELERY_DISPATCH_BATCH_SIZE=10" in content
