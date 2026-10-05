from unittest import mock

import pytest

import daedalus


@pytest.fixture
def manager_cls(monkeypatch):
    """Replace the manager so main()'s dispatch can be checked in isolation."""
    cls = mock.MagicMock(name="LabelBasedContainerManager")
    monkeypatch.setattr(daedalus, "LabelBasedContainerManager", cls)
    return cls


@pytest.mark.parametrize(
    "argv, method, args, kwargs",
    [
        (["build"], "build", ("latest",), {}),
        (["build", "--tag", "v2"], "build", ("v2",), {}),
        (
            ["run", "--pwd", "/src", "--hostname", "me@box", "--name", "dev", "--rm"],
            "run",
            (),
            {"pwd": "/src", "hostname": "me@box", "name": "dev", "remove": True},
        ),
        (["run"], "run", (), {"pwd": None, "hostname": None, "name": None, "remove": False}),
        (["list"], "list_containers", (), {"filter_by_hostname": None}),
        (
            ["list", "--filter-hostname", "me@box"],
            "list_containers",
            (),
            {"filter_by_hostname": "me@box"},
        ),
        (["inspect", "--container-id", "abc"], "inspect", ("abc",), {}),
        (["stop", "--label-key", "app.pwd", "--label-value", "/src"], "stop_by_label", ("app.pwd", "/src"), {}),
        (
            ["remove", "--label-key", "app.pwd", "--label-value", "/src", "--force"],
            "remove_by_label",
            ("app.pwd", "/src", True),
            {},
        ),
        (
            ["remove", "--label-key", "app.pwd", "--label-value", "/src"],
            "remove_by_label",
            ("app.pwd", "/src", False),
            {},
        ),
        (["clean"], "clean_image", ("latest",), {}),
        (["clean", "--tag", "v2"], "clean_image", ("v2",), {}),
        (["logs", "--container-id", "abc"], "logs", ("abc",), {}),
    ],
)
def test_actions_dispatch_to_the_manager(cli, manager_cls, argv, method, args, kwargs):
    getattr(manager_cls.return_value, method).return_value = True

    assert cli(*argv) == 0
    getattr(manager_cls.return_value, method).assert_called_once_with(*args, **kwargs)


def test_app_name_is_passed_to_the_manager(cli, manager_cls):
    cli("list", "--app", "other")

    manager_cls.assert_called_once_with(app_name="other")


def test_default_app_name_is_alpine_dev(cli, manager_cls):
    cli("list")

    manager_cls.assert_called_once_with(app_name="alpine-dev")


def test_failed_action_exits_1(cli, manager_cls):
    manager_cls.return_value.build.return_value = False

    assert cli("build") == 1


@pytest.mark.parametrize(
    "argv, message",
    [
        (["inspect"], "Error: --container-id required"),
        (["logs"], "Error: --container-id required"),
        (["stop"], "Error: --label-key and --label-value required"),
        (["stop", "--label-key", "k"], "Error: --label-key and --label-value required"),
        (["remove", "--label-value", "v"], "Error: --label-key and --label-value required"),
    ],
)
def test_missing_required_options_exit_1(cli, manager_cls, capsys, argv, message):
    assert cli(*argv) == 1
    assert message in capsys.readouterr().err
    method_calls = [c for c in manager_cls.return_value.method_calls]
    assert method_calls == []


def test_unknown_action_is_a_usage_error(cli, manager_cls):
    assert cli("explode") == 2


def test_dockerfile_option_was_removed(cli, manager_cls):
    """The Containerfile is embedded, so --dockerfile is no longer accepted."""
    assert cli("build", "--dockerfile", "Containerfile") == 2


def test_keyboard_interrupt_exits_cleanly(cli, manager_cls, capsys):
    manager_cls.return_value.run.side_effect = KeyboardInterrupt

    assert cli("run") == 0
    assert "Interrupted by user" in capsys.readouterr().out


def test_unexpected_error_exits_1(cli, manager_cls, capsys):
    manager_cls.return_value.list_containers.side_effect = RuntimeError("boom")

    assert cli("list") == 1
    assert "Unexpected error: boom" in capsys.readouterr().err
