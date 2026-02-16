# Coming Soon

Features planned but not yet built.

## VPS Deployment
- Systemd service file for `--listen` mode
- Setup script (install Node, Claude Code, Playwright deps)
- Auth token refresh automation

## Auto-Deploy
- `vercel deploy` after build completes
- Preview URL sent via Telegram notification
- Support Cloudflare Pages as alternative

## Budget Alerts
- Mid-build notification: "Spent $50 of $200 budget. Continue? Y/N"
- Per-story cost tracking in SPRINTS.json
- Auto-pause when budget threshold hit (e.g., 80%)

## Architecture Review Checkpoints
- Periodic human review at sprint boundaries (not just per-story)
- Agent generates architecture summary for review
- Human approves or requests refactor before next sprint

## Multi-Project Support
- Run 2+ builds simultaneously from `--listen` mode
- Per-project agent pools with shared rate limit awareness
- Project queue with priority
