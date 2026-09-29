from datetime import datetime
from pathlib import Path

from audial_mcp.results import inventory, list_jobs, new_job_dir, slugify


def test_slugify_strips_and_truncates():
    assert slugify("My Demo (final).wav") == "My_Demo_final_wav"
    assert slugify("   ") == "job"
    assert len(slugify("x" * 100)) == 40


def test_new_job_dir_layout(tmp_path):
    d = new_job_dir(tmp_path, "stem_split", "demo", now=datetime(2026, 9, 29, 10, 45, 12))
    assert d == tmp_path / "stem_split" / "2026-09-29_104512_demo"
    assert d.is_dir()


def test_inventory_is_recursive_sorted_and_skips_hidden(tmp_path):
    (tmp_path / "b.wav").write_bytes(b"12")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.mid").write_bytes(b"1")
    (tmp_path / ".DS_Store").write_bytes(b"x")
    files = inventory(tmp_path)
    assert [f["name"] for f in files] == ["b.wav", "sub/a.mid"]
    assert files[0]["size_bytes"] == 2 and Path(files[0]["path"]).is_absolute()


def test_list_jobs_newest_first_and_filtered(tmp_path):
    older = new_job_dir(tmp_path, "stem_split", "one", now=datetime(2026, 9, 1, 0, 0, 0))
    newer = new_job_dir(tmp_path, "stem_split", "two", now=datetime(2026, 9, 2, 0, 0, 0))
    other = new_job_dir(tmp_path, "analyze", "three", now=datetime(2026, 9, 3, 0, 0, 0))
    for d in (older, newer, other):
        (d / "out.wav").write_bytes(b"0")
    jobs = list_jobs(tmp_path, tool=None, limit=10)
    assert [j["output_dir"] for j in jobs] == [str(other), str(newer), str(older)]
    only_stems = list_jobs(tmp_path, tool="stem_split", limit=1)
    assert len(only_stems) == 1 and only_stems[0]["output_dir"] == str(newer)
    assert only_stems[0]["files"][0]["name"] == "out.wav"


def test_list_jobs_empty_when_dir_missing(tmp_path):
    assert list_jobs(tmp_path / "nope", tool=None, limit=5) == []
