# Security Policy

## Supported versions

Only the latest released tag receives fixes. `main` is unstable.

## Reporting a vulnerability

Report suspected vulnerabilities privately. Do **not** open a public issue.

- Preferred: GitHub's **Security → Report a vulnerability** tab on this
  repository (Private Vulnerability Reporting).
- Fallback: the maintainer's contact address is in this project's package
  metadata (`pyproject.toml`); use `keycut security` as the subject. Encrypted
  mail is welcome — ask for a key in a first, contentless message.

Please include the affected version or commit, what happens and why it matters,
reproduction steps, and any suggested fix.

## What to expect

- Acknowledgement within 5 business days.
- Initial assessment and severity triage within 10 business days.
- Coordinated disclosure: a timeline agreed with you before any public write-up,
  and credit if you want it.

## Scope and threat model

keycut builds `ffmpeg` and `ffprobe` argument lists and runs them with
`subprocess.run` using a **list argv — never a shell**. File paths reach ffmpeg
as single arguments, so a path containing shell metacharacters is not
interpreted. The one place a path is embedded in a text format is the concat
demuxer manifest, where single quotes are escaped.

**In scope:** anything that lets a caller's input escape the argv list, corrupt
the concat manifest, or cause keycut to read or write a path it was not given.

**Out of scope:**

- Vulnerabilities in ffmpeg or ffprobe themselves. keycut decides *which*
  arguments to pass; it does not parse media. Report those upstream to the
  FFmpeg project.
- Consequences of pointing `ffmpeg_bin` / `ffprobe_bin` at an untrusted
  executable. Those parameters exist so you can choose your own build; keycut
  runs what you name.
- Processing a malicious media file. That is ffmpeg's parser, running with
  whatever privileges you gave it. Decode untrusted media in a sandbox.
