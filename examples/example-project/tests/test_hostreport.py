from pathlib import Path

from hostreport import collect, parse_os_release

SAMPLE = """\
NAME="Rocky Linux"
ID="rocky"
# comment
VERSION_ID="9.4"
PRETTY_NAME="Rocky Linux 9.4 (Blue Onyx)"
"""


def test_parse_os_release():
    info = parse_os_release(SAMPLE)
    assert info["ID"] == "rocky"
    assert info["PRETTY_NAME"] == "Rocky Linux 9.4 (Blue Onyx)"


def test_collect_uses_os_release(tmp_path: Path):
    f = tmp_path / "os-release"
    f.write_text(SAMPLE)
    facts = collect(f)
    assert facts["os_id"] == "rocky"
    assert facts["python"]
