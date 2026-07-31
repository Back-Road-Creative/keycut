"""The ``keycut`` command line."""

from __future__ import annotations

import pytest

from conftest import fake_ffmpeg, keyframe_csv
from keycut.cli import main, parse_timecode


class TestParseTimecode:
    @pytest.mark.parametrize(
        ("text", "seconds"),
        [
            ("0", 0.0),
            ("90", 90.0),
            ("90.5", 90.5),
            ("1:30", 90.0),
            ("01:02:03", 3723.0),
            ("0:00:01.5", 1.5),
        ],
    )
    def test_accepted_forms(self, text, seconds):
        assert parse_timecode(text) == pytest.approx(seconds)

    @pytest.mark.parametrize("text", ["", "1:2:3:4", "abc", "1::2", "1:"])
    def test_rejected_forms(self, text):
        with pytest.raises(ValueError):
            parse_timecode(text)


class TestMain:
    def test_a_successful_run_prints_the_output_path(self, tmp_path, capsys):
        master = tmp_path / "m.mp4"
        master.write_bytes(b"x")
        out = tmp_path / "out.mp4"
        with fake_ffmpeg():
            code = main([str(master), str(out), "-r", "0:10", "0:40"])
        assert code == 0
        assert capsys.readouterr().out.strip() == str(out)

    def test_ranges_are_parsed_as_timecodes(self, tmp_path):
        master = tmp_path / "m.mp4"
        master.write_bytes(b"x")
        with fake_ffmpeg() as fake:
            main([str(master), str(tmp_path / "o.mp4"), "-r", "1:00", "1:30"])
        cmd = fake.extract_commands[0]
        assert float(cmd[cmd.index("-to") + 1]) == 90.0

    def test_exclusions_become_a_per_second_mask(self, tmp_path):
        # Sparse keyframes force a snap-back over the excluded window, so the
        # exclusion has to promote this range to a re-encode.
        master = tmp_path / "m.mp4"
        master.write_bytes(b"x")
        with fake_ffmpeg(keyframe_stdout=b"0.000000,K__\n500.000000,K__\n") as fake:
            main(
                [
                    str(master), str(tmp_path / "o.mp4"),
                    "-r", "100", "160",
                    "-x", "40", "60",
                ]
            )
        assert "-c:v" in fake.extract_commands[0]

    def test_no_range_is_an_error(self, tmp_path):
        master = tmp_path / "m.mp4"
        master.write_bytes(b"x")
        with pytest.raises(SystemExit, match="at least one --range"):
            main([str(master), str(tmp_path / "o.mp4")])

    def test_an_inverted_range_is_rejected_before_ffmpeg_runs(self, tmp_path):
        master = tmp_path / "m.mp4"
        master.write_bytes(b"x")
        with pytest.raises(SystemExit, match="ends at or before it starts"):
            main([str(master), str(tmp_path / "o.mp4"), "-r", "60", "30"])

    def test_a_failed_extraction_exits_nonzero(self, tmp_path):
        master = tmp_path / "m.mp4"
        master.write_bytes(b"x")
        with fake_ffmpeg(returncode=1):
            assert main([str(master), str(tmp_path / "o.mp4"), "-r", "10", "20"]) == 1

    def test_dry_run_prints_the_plan_without_calling_ffmpeg(self, tmp_path, capsys):
        master = tmp_path / "m.mp4"
        master.write_bytes(b"x")
        with fake_ffmpeg(keyframe_stdout=keyframe_csv(interval=2.0, count=200)) as fake:
            code = main(
                [
                    str(master), str(tmp_path / "o.mp4"),
                    "-r", "10", "40",
                    "-r", "41", "70",
                    "--dry-run",
                ]
            )
        out = capsys.readouterr().out
        assert code == 0
        assert fake.extract_commands == []
        assert "2 range(s) -> 1 extraction(s)" in out
        assert "stream copy" in out

    def test_dry_run_reports_unknown_keyframes(self, tmp_path, capsys):
        master = tmp_path / "m.mp4"
        master.write_bytes(b"x")
        with fake_ffmpeg(keyframe_stdout=b""):
            main([str(master), str(tmp_path / "o.mp4"), "-r", "10", "40", "--dry-run"])
        out = capsys.readouterr().out
        assert "keyframes: UNKNOWN" in out
        assert "re-encode" in out
