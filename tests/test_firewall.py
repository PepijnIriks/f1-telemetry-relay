"""The firewall rule must work with old and new Azure CLI versions (option names were renamed)."""
import azure_firewall as fw


def fake_cli(new_style):
    calls = []

    def run(args, timeout=60):
        calls.append(args)
        if new_style and "--rule-name" in args:
            return 2, "", "ERROR: unrecognized arguments: --rule-name x"
        if not new_style and "--server-name" in args:
            return 2, "", "ERROR: unrecognized arguments: --server-name f1db"
        return 0, "", ""
    return run, calls


def test_new_cli_uses_server_name():
    fw._STYLE["v"] = None
    run, calls = fake_cli(new_style=True)
    rc, _o, _e = fw.rule_cmd(run, "create", "f1db", "relay-PC", ["--start-ip-address", "1.2.3.4"])
    assert rc == 0 and len(calls) == 1
    assert calls[0][calls[0].index("--server-name") + 1] == "f1db"
    assert calls[0][calls[0].index("--name") + 1] == "relay-PC"


def test_old_cli_falls_back_and_remembers():
    fw._STYLE["v"] = None
    run, calls = fake_cli(new_style=False)
    rc, _o, _e = fw.rule_cmd(run, "create", "f1db", "relay-PC")
    assert rc == 0 and len(calls) == 2
    assert calls[1][calls[1].index("--rule-name") + 1] == "relay-PC"
    fw.rule_cmd(run, "show", "f1db", "relay-PC")
    assert len(calls) == 3 and "--rule-name" in calls[2]     # no second guess needed


def test_real_error_is_not_retried():
    fw._STYLE["v"] = None
    calls = []

    def run(args, timeout=60):
        calls.append(args)
        return 1, "", "Code: InternalServerError"
    rc, _o, err = fw.rule_cmd(run, "create", "f1db", "relay-PC")
    assert rc == 1 and "InternalServerError" in err and len(calls) == 1
