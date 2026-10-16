# Submission: Software Systems Engineer case assessment ("Never Lose a Heartbeat")

**Candidate:** Khushi Gupta, B.Tech CSE, VIT Bhopal

**Repository:** https://github.com/KGuptaCyb/never-lose-a-heartbeat

| Item asked for in the brief | Where to find it |
|---|---|
| 1. Code repository with a README to install and run on a fresh board | `README.md`, `deploy/install.sh` |
| 2. Design note (at most 2 pages) | `docs/DESIGN.pdf` (source: `docs/DESIGN.md`) |
| 3. Evidence of failure tests and their results | `evidence/SUMMARY.md` and the logs in `evidence/` |
| 4. Resource figures over 1 hour in online mode, ARM64 | `evidence/resources_1h_online.json`, summarised in `evidence/SUMMARY.md` |
| 5. Demo video | `docs/DEMO.md` gives the script (network cut, process kill, power cut). The recording is included in this zip if present. |

**Environment.** An ARM64 Ubuntu VM limited to 4 vCPUs and 2 GB RAM (Lima, Apple Virtualization), and the Supabase free tier.

**Mock service.** The brief's download link for `biosignal-mock` was blank, so the pipeline was tested against `tools/fake_mock.py`. This is a stand-in written from section 6 of the brief.

**AI disclosure.** Built with the help of Claude (Anthropic) via Claude Code, as stated in the README.
