from pathlib import Path

from codex_gui.models import AccessMode, Attachment, ModelInfo, ThreadSummary, weekly_limit_from_payload


def test_access_modes_produce_protocol_sandbox_objects() -> None:
    assert AccessMode.READ_ONLY.sandbox_policy("/repo") == {
        "type": "readOnly",
        "networkAccess": False,
    }
    assert AccessMode.WORKSPACE_WRITE.sandbox_policy("/repo") == {
        "type": "workspaceWrite",
        "writableRoots": ["/repo"],
        "networkAccess": False,
    }
    assert AccessMode.FULL_ACCESS.sandbox_policy("/repo") == {"type": "dangerFullAccess"}


def test_attachments_map_to_app_server_inputs() -> None:
    assert Attachment(Path("/tmp/a.png"), True).as_user_input() == {
        "type": "localImage",
        "path": "/tmp/a.png",
    }
    assert Attachment(Path("/tmp/main.py"), False).as_user_input() == {
        "type": "mention",
        "name": "main.py",
        "path": "/tmp/main.py",
    }


def test_model_and_thread_wire_parsing() -> None:
    model = ModelInfo.from_wire(
        {
            "model": "gpt-test",
            "displayName": "GPT Test",
            "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}],
            "defaultReasoningEffort": "high",
            "inputModalities": ["text"],
            "isDefault": True,
        }
    )
    assert model.id == "gpt-test"
    assert model.efforts == ["low", "high"]
    assert model.modalities == {"text"}

    thread = ThreadSummary.from_wire({"id": "thr_1", "preview": "  Fix   the bug  ", "cwd": "/repo"})
    assert thread.title == "Fix the bug"


def test_weekly_rate_limit_is_selected_and_converted_to_remaining_percent() -> None:
    window = weekly_limit_from_payload(
        {
            "rateLimits": {
                "primary": {"usedPercent": 18, "windowDurationMins": 300, "resetsAt": 1},
                "secondary": {"usedPercent": 37.4, "windowDurationMins": 10080, "resetsAt": 99},
            }
        }
    )
    assert window is not None
    assert window.remaining_percent == 63
    assert window.resets_at == 99


def test_rate_limit_parser_does_not_mislabel_short_window_as_weekly() -> None:
    assert weekly_limit_from_payload(
        {"rateLimits": {"primary": {"usedPercent": 20, "windowDurationMins": 300}}}
    ) is None
